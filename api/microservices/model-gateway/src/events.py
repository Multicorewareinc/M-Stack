"""Event backbone publisher (ADR-007/008/009).

The gateway's async strain: publish a canonical event per request/response to the event
backbone through the `multistack.queue` SDK seam (ADR-033), always-on but config-gated
(inert when EVENT_BACKBONE_URL is unset). This never sits on the client's latency path —
`emit` schedules an isolated background task and returns immediately, and is TOTAL (it can
never raise onto the request/stream path). A down/unreachable backbone drops+logs; it never
blocks or fails a request, and a failed startup connect leaves the publisher degraded
rather than crashing the gateway.

The SDK is imported lazily inside connect() so the module (and the inert path + offline
tests) load without it present; a failed import leaves the publisher degraded, exactly like
a down backbone. Which backend the SDK talks to is env-driven (QUEUE_BACKEND); this service
no longer imports any backend client (e.g. nats) directly. The SDK serializes the event dict
and maps `PublishOptions.message_id` onto the backbone's dedup header.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from prometheus_client import Counter

logger = logging.getLogger(__name__)

# How long gateway startup may wait for the backbone before continuing degraded (ADR-007).
# See connect() for why this bound is required rather than relying on connect() returning.
_CONNECT_TIMEOUT_SECONDS = 10

# Module-level (not per-instance) so repeated create_app() in tests never double-registers.
PUBLISHED = Counter("gw_events_published_total", "Backbone events published", ["type"])
DROPPED = Counter("gw_events_dropped_total", "Backbone events dropped", ["reason"])


def now_ts() -> float:
    return time.time()


# --- event builders ---------------------------------------------------------
def request_event(ctx: dict, body: dict | None) -> dict:
    ev = {
        "type": "request",
        "request_id": ctx["request_id"],
        "principal": ctx.get("principal"),
        "api_key_id": ctx.get("api_key_id"),
        "owner_id": ctx.get("owner_id"),
        "model": ctx.get("model"),
        "path": ctx.get("path"),
        "ts": now_ts(),
    }
    if body is not None:
        ev["body"] = body
    return ev


def chunk_event(request_id: str, seq: int, chunk: bytes, capture: bool) -> dict:
    ev = {"type": "chunk", "request_id": request_id, "seq": seq, "ts": now_ts()}
    if capture:
        ev["data"] = chunk.decode("utf-8", "ignore")
    return ev


def response_event(
    ctx: dict,
    *,
    status: int,
    usage: dict | None,
    duration_ms: float,
    stream: bool,
    partial: bool | None = None,
    chunks: int | None = None,
    body: dict | None = None,
) -> dict:
    ev = {
        "type": "response",
        "request_id": ctx["request_id"],
        "principal": ctx.get("principal"),
        "api_key_id": ctx.get("api_key_id"),
        "owner_id": ctx.get("owner_id"),
        "model": ctx.get("model"),
        "status": status,
        "usage": usage,
        "ts": now_ts(),
        "duration_ms": round(duration_ms, 3),
        "stream": stream,
    }
    if partial is not None:
        ev["partial"] = partial
    if chunks is not None:
        ev["chunks"] = chunks
    if body is not None:
        ev["body"] = body
    return ev


# --- streaming line helpers (raw byte chunks split arbitrarily) --------------
def split_complete_lines(buf: bytes) -> tuple[bytes, list[str]]:
    """Return (leftover tail, list of complete newline-terminated lines as str)."""
    if b"\n" not in buf:
        return buf, []
    *complete, tail = buf.split(b"\n")
    return tail, [ln.decode("utf-8", "ignore") for ln in complete]


def usage_from_sse_line(line: str) -> dict | None:
    """Parse `usage` from an SSE `data:` line only when it plausibly carries it."""
    s = line.strip()
    if not s.startswith("data:") or "usage" not in s:
        return None
    payload = s[len("data:") :].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        obj = json.loads(payload)
    except ValueError:
        return None
    usage = obj.get("usage") if isinstance(obj, dict) else None
    return usage if isinstance(usage, dict) else None


def is_terminal(line: str) -> bool:
    return line.strip() == "data: [DONE]"


def delta_text_from_sse_line(line: str) -> str | None:
    """Extract one chunk's incremental assistant text from an SSE `data:` line. The gateway
    always emits OpenAI-shaped stream chunks (choices[0].delta.content) — adapters.translate_stream
    normalizes an Anthropic upstream to the same shape before it ever reaches here — so this
    needs no provider branching, unlike usage_from_sse_line which reads the raw upstream line."""
    s = line.strip()
    if not s.startswith("data:"):
        return None
    payload = s[len("data:") :].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        obj = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    choices = obj.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    delta = choices[0].get("delta")
    content = delta.get("content") if isinstance(delta, dict) else None
    return content if isinstance(content, str) else None


class EventPublisher:
    def __init__(
        self,
        url: str,
        *,
        stream_name: str,
        subject: str,
        capture_bodies: bool,
        max_inflight: int,
        backend: str = "nats",
    ) -> None:
        self.url = url
        self.backend = backend
        self.stream_name = stream_name
        self.subject = subject
        self.capture_bodies = capture_bodies
        self.max_inflight = max_inflight
        self._queue = None
        self._tasks: set[asyncio.Task] = set()

    @property
    def connected(self) -> bool:
        """Whether the backbone connection is live (the SDK's own view)."""
        return bool(self._queue is not None and self._queue.connected)

    async def connect(self) -> None:
        """Best-effort connect through the multistack.queue SDK. A down backbone logs and
        continues degraded (self._queue stays None => emits drop) rather than crashing gateway
        startup (ADR-007). Stream existence is the backbone's responsibility (NACK-provisioned),
        not the publisher's."""
        try:
            from multistack.queue import Queue, QueueConfig  # lazy: inert path / offline tests

            queue = Queue(QueueConfig(name=self.backend, url=self.url))
            # Bounded: the SDK retries its INITIAL connect internally and indefinitely
            # (NatsConfig.retry_on_initial_connect defaults True, and QueueConfig exposes no
            # knob to turn it off), so an unreachable backbone would hang gateway startup and
            # leave it not listening at all. ADR-007 wants degraded, never blocked.
            await asyncio.wait_for(queue.connect(), timeout=_CONNECT_TIMEOUT_SECONDS)
            self._queue = queue
        except Exception:
            logger.warning("event_backbone_connect_failed_degraded", exc_info=True)
            self._queue = None

    def emit(self, event: dict) -> None:
        """Fire-and-forget. Sync, returns immediately, and TOTAL — never raises onto the
        request/stream path."""
        try:
            if self._queue is None:
                DROPPED.labels("not_connected").inc()
                return
            if len(self._tasks) >= self.max_inflight:
                DROPPED.labels("over_inflight").inc()
                return
            task = asyncio.create_task(self._safe_publish(event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except Exception:
            logger.warning("event_emit_failed", exc_info=True)
            DROPPED.labels("emit_error").inc()

    async def _safe_publish(self, event: dict) -> None:
        try:
            # FIXED: request and response events shared a bare request_id as their
            # Nats-Msg-Id, identical to each other. JetStream's duplicate window (keyed only
            # on Nats-Msg-Id, not message content) silently discarded the response event as a
            # "duplicate" of the request event published moments earlier for the same call --
            # it never reached the durable stream at all, only ephemeral core subscribers
            # (which aren't gated by JetStream's storage-layer dedup). Every downstream
            # consumer of `response` events (billing, the enricher, TPM pre-ADR-030) was
            # silently starved of them from day one. Every event type now gets its own
            # suffix, same idea chunk_event already used for its per-chunk seq.
            from multistack.queue import PublishOptions

            seq = event.get("seq")
            if event["type"] == "chunk":
                msg_id = f"{event['request_id']}:{seq}"
            else:
                msg_id = f"{event['request_id']}:{event['type']}"
            # The SDK serializes the dict itself (json.dumps(..., default=str)) and maps
            # PublishOptions.message_id onto the Nats-Msg-Id dedup header. publish() raises on
            # failure; this method is the gateway's own total boundary — it catches below and
            # counts the drop, so a backbone fault never reaches the request/stream path.
            await self._queue.publish(
                self.subject,
                event,
                options=PublishOptions(message_id=str(msg_id)),
            )
            PUBLISHED.labels(event["type"]).inc()
        except Exception:
            logger.warning("event_publish_failed", exc_info=True)
            DROPPED.labels("publish_error").inc()

    async def aclose(self) -> None:
        if self._tasks:
            try:
                await asyncio.wait(self._tasks, timeout=2.0)
            except Exception:
                logger.warning("event_drain_failed", exc_info=True)
        if self._queue is not None:
            try:
                await self._queue.close()
            except Exception:
                logger.warning("event_backbone_close_failed", exc_info=True)
