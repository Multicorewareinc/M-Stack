"""Event backbone publisher (ADR-007/008/009).

The gateway's async strain: publish a canonical event per request/response to NATS
JetStream, always-on but config-gated (inert when EVENT_BACKBONE_URL is unset). This
never sits on the client's latency path — `emit` schedules an isolated background task
and returns immediately, and is TOTAL (it can never raise onto the request/stream path).
A down/unreachable backbone drops+logs; it never blocks or fails a request, and a failed
startup connect leaves the publisher degraded rather than crashing the gateway.

`nats` is imported lazily inside connect()/_safe_publish so the module (and the inert
path + offline tests) load without nats-py installed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from prometheus_client import Counter

logger = logging.getLogger(__name__)

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
    payload = s[len("data:"):].strip()
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


class EventPublisher:
    def __init__(
        self,
        url: str,
        *,
        stream_name: str,
        subject: str,
        capture_bodies: bool,
        max_inflight: int,
    ) -> None:
        self.url = url
        self.stream_name = stream_name
        self.subject = subject
        self.capture_bodies = capture_bodies
        self.max_inflight = max_inflight
        self._nc = None
        self._js = None
        self._tasks: set[asyncio.Task] = set()

    async def connect(self) -> None:
        """Best-effort connect. A down backbone logs and continues degraded (js stays
        None => emits drop) rather than crashing gateway startup (ADR-007)."""
        try:
            import nats  # lazy: inert path / offline tests don't need nats-py

            self._nc = await nats.connect(self.url)
            self._js = self._nc.jetstream()
            try:
                await self._js.add_stream(name=self.stream_name, subjects=[self.subject])
            except Exception:
                logger.info("event_stream_exists", extra={"stream": self.stream_name})
        except Exception:
            logger.warning("event_backbone_connect_failed_degraded", exc_info=True)
            self._nc = None
            self._js = None

    def emit(self, event: dict) -> None:
        """Fire-and-forget. Sync, returns immediately, and TOTAL — never raises onto the
        request/stream path."""
        try:
            if self._js is None:
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
            seq = event.get("seq")
            if event["type"] == "chunk":
                msg_id = f"{event['request_id']}:{seq}"
            else:
                msg_id = f"{event['request_id']}:{event['type']}"
            await self._js.publish(
                self.subject,
                json.dumps(event, default=str).encode(),
                headers={"Nats-Msg-Id": str(msg_id)},
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
        if self._nc is not None:
            try:
                await self._nc.close()
            except Exception:
                logger.warning("event_backbone_close_failed", exc_info=True)
