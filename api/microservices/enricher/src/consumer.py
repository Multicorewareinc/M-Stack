"""Event enricher stage (ADR-030).

Consumes `response` events from the gateway's `gateway.events` (stream GATEWAY_EVENTS),
guarantees a normalized token count via `resolve_usage`, and republishes to a separate
`gateway.events.enriched` (stream GATEWAY_EVENTS_ENRICHED) with the message id set to
`request_id` (the backbone's dedup key). Only `response` events are enriched;
`request`/`chunk` are skipped.

Queue I/O goes through the multistack.queue SDK (ADR-033), not a backend client: the SDK is
imported lazily (offline tests degrade gracefully) and owns deserialization, acknowledgement
and reconnection. A single Queue serves both roles here — consuming the raw stream and
republishing the enriched one. The durable consumer and both streams are provisioned
externally by NACK; this service never creates them.

Ack policy DIVERGES from the rate limiters' always-ack (ADR-030 D5): the enricher is the SOLE
producer of a stream billing depends on, so a dropped republish is real data loss. It binds
with AckMode.ON_SUCCESS, which means returning acks and RAISING withholds the ack so the
backbone redelivers (bounded by the max_deliver/ack_wait on the NACK consumer). An
un-parseable message is acked by the SDK driver before it reaches the handler, so a poison
message still cannot stall the consumer. Recovery after terminal failure is operator-driven
replay from the retained input stream.

Known gap (ADR-033): terminal exhaustion is no longer observable in-process — the handler
receives only the payload dict, with no delivery count — so REPUBLISH_FAIL is labelled
terminal="unknown". See the TODO in _on_msg.
"""

from __future__ import annotations

import asyncio
import logging

from prometheus_client import Counter
from service import resolve_usage

logger = logging.getLogger(__name__)

# How long startup may wait for the backbone before giving up and handing off to the
# background retry. The SDK retries its INITIAL connect internally and indefinitely
# (NatsConfig.retry_on_initial_connect defaults to True, 1s->30s backoff) and QueueConfig
# exposes no knob to turn that off — so `await queue.connect()` on an unreachable backbone
# never returns, which would hang lifespan startup and leave the service not listening at
# all. Bounding it keeps ADR-016's promise: a down backbone leaves the consumer inactive,
# never blocks or crashes startup. _retry_connect() below owns the waiting instead.
_CONNECT_TIMEOUT_SECONDS = 10

# Module-level (not per-instance) so repeated create_app() in tests never double-registers.
EVENTS = Counter("enricher_events_total", "Enriched events by usage source", ["source", "reason"])
REPUBLISH_FAIL = Counter(
    # terminal="unknown" since ADR-033: the SDK's handler contract carries no delivery count,
    # so a first failure and the max_deliver-exhausting one are indistinguishable here.
    "enricher_republish_failures_total",
    "Republish failures (terminal=unknown since ADR-033)",
    ["terminal"],
)
TOKENIZER_FALLBACK = Counter(
    "enricher_tokenizer_fallback_total", "Tokenizer fallback outcomes", ["outcome"]
)

# Fields carried through from the source `response` event onto the enriched event.
# `body` is deliberately dropped; `usage` is replaced with the normalized object.
_PRESERVED = (
    "request_id",
    "principal",
    "model",
    "status",
    "ts",
    "duration_ms",
    "stream",
    # Billing attribution (add-enricher-usage-attribution) — carried through from the gateway
    # event unchanged so downstream (billing) can attribute usage per key/user; absent on
    # pre-attribution source events, in which case .get() below yields None (never fabricated).
    "api_key_id",
    "owner_id",
)


class _RepublishError(Exception):
    """Raised when publishing an enriched event fails, to trigger the no-ack/redeliver path."""


class EnricherConsumer:
    def __init__(self, settings, *, http_client=None, queue=None) -> None:
        self._url = settings.event_backbone_url
        self._backend = settings.queue_backend
        self._in_stream = settings.event_stream_name
        self._durable = settings.event_durable_name
        self._out_stream = settings.enriched_stream_name
        self._out_subject = settings.enriched_subject
        # No _max_deliver/_ack_wait here any more: redelivery bounds live on the
        # NACK-provisioned consumer, and the handler gets no delivery count to compare
        # against anyway (see the TODO in _on_msg). The settings keep the values as the
        # documented contract for whoever writes the NACK manifest.
        self._tokenizer_url = settings.tokenizer_url
        self._tokenizer_timeout_ms = settings.tokenizer_timeout_ms
        self._http_client = http_client
        # ONE Queue serves both roles: this service consumes the raw stream and republishes
        # the enriched one, and the SDK's Queue supports publish and subscribe together.
        self._queue = queue  # test override; production builds it in start()
        self._bound = False
        # Hold a strong ref to the background retry task: the loop keeps only a weak
        # reference, so an un-stored task can be GC'd mid-backoff (RUF006).
        self._retry_task: asyncio.Task | None = None

    @property
    def active(self) -> bool:
        """True once the durable consumer is bound (enriching/republishing is live)."""
        return self._bound

    async def start(self) -> None:
        """Best-effort connect through the multistack.queue SDK. A down backbone leaves the
        consumer inactive rather than crashing startup, while a background task keeps retrying
        until it succeeds (_retry_connect). Reconnect after a first successful connect is the
        SDK's own job (its NATS client reconnects and restores the push subscription)."""
        try:
            from multistack.queue import Queue, QueueConfig  # lazy: offline tests degrade

            queue = Queue(QueueConfig(name=self._backend, url=self._url))
            await asyncio.wait_for(queue.connect(), timeout=_CONNECT_TIMEOUT_SECONDS)
            self._queue = queue
        except Exception:
            logger.warning("enricher_connect_failed_retrying", exc_info=True)
            self._queue = None
            self._retry_task = asyncio.create_task(self._retry_connect())
            return
        await self._bind()

    async def _retry_connect(self) -> None:
        """Background retry for a backbone that was down when start() ran. Capped exponential
        backoff (1s, 2s, 4s, ... up to 30s) until connect() succeeds, then binds directly."""
        delay = 1
        while self._queue is None:
            await asyncio.sleep(delay)
            try:
                from multistack.queue import Queue, QueueConfig

                queue = Queue(QueueConfig(name=self._backend, url=self._url))
                await queue.connect()
                self._queue = queue
            except Exception:
                delay = min(delay * 2, 30)
        await self._bind()

    async def _bind(self) -> None:
        """Bind to the NACK-provisioned durable consumer, classifying failures.

        AckMode.ON_SUCCESS is what preserves ADR-030 D5: the SDK acks only when the handler
        returns, so a handler that RAISES (a failed republish) leaves the message
        unacknowledged and the backbone redelivers it — bounded by the max_deliver/ack_wait
        configured on the NACK consumer, not here."""
        try:
            from multistack.queue.drivers.nats.messaging import AckMode, ConsumerBinding

            await self._queue.subscribe(
                ConsumerBinding(
                    stream=self._in_stream,
                    durable=self._durable,
                    ack_mode=AckMode.ON_SUCCESS,
                ),
                self._on_msg,
            )
            self._bound = True
            logger.info(
                "enricher_started", extra={"stream": self._in_stream, "durable": self._durable}
            )
        except Exception as exc:
            self._bound = False
            text = str(exc).lower()
            if "already" in text and ("exist" in text or "bound" in text or "in use" in text):
                logger.warning(
                    "enricher_durable_conflict", exc_info=True
                )  # another replica holds it
            else:
                logger.warning(
                    "enricher_bind_failed", exc_info=True
                )  # transient — retried on reconnect

    async def _build_enriched(self, event: dict) -> dict:
        usage = await resolve_usage(
            event,
            http_client=self._http_client,
            tokenizer_url=self._tokenizer_url,
            timeout_ms=self._tokenizer_timeout_ms,
        )
        EVENTS.labels(source=usage["source"], reason=usage.get("reason", "-")).inc()
        if usage["source"] == "estimated":
            TOKENIZER_FALLBACK.labels("ok").inc()
        elif usage.get("reason") == "tokenizer_error":
            TOKENIZER_FALLBACK.labels("error").inc()
        enriched = {"type": "response", "usage": usage}
        for field in _PRESERVED:
            enriched[field] = event.get(field)
        return enriched

    async def _publish_enriched(self, enriched: dict) -> None:
        try:
            from multistack.queue import PublishOptions

            # publish() propagates a real failure — the enricher DEPENDS on that: a failed
            # republish must raise so _on_msg re-raises and the SDK (AckMode.ON_SUCCESS)
            # withholds the ack, letting the backbone redeliver. The SDK serializes the dict
            # and maps message_id (== request_id) onto the dedup header the backbone uses to
            # drop duplicate republishes.
            await self._queue.publish(
                self._out_subject,
                enriched,
                options=PublishOptions(message_id=str(enriched["request_id"])),
            )
        except Exception as exc:
            raise _RepublishError() from exc

    async def _on_msg(self, event: dict) -> None:
        """Handle one already-deserialized gateway event.

        The SDK owns deserialization (a malformed payload is logged and acked by the driver
        before it reaches here) and acknowledgement. Under AckMode.ON_SUCCESS:

          - return normally -> acked (nothing to republish, or republish succeeded);
          - RAISE           -> not acked, so the backbone redelivers (ADR-030 D5). This is the
                               successor to the old "do not call msg.ack()" path.
        """
        if event.get("type") != "response":
            return  # request/chunk/other: skip, nothing to republish (acked)

        try:
            enriched = await self._build_enriched(event)
        except Exception:
            # A build fault is not fixable by redelivery (same input, same failure), so this
            # returns -> acked, exactly as the pre-SDK code acked it.
            logger.warning("enricher_build_error_acked", exc_info=True)
            return

        try:
            await self._publish_enriched(enriched)
        except _RepublishError:
            # ponytail: terminal-exhaustion detection is currently unobservable. The old code
            # read msg.metadata.num_delivered to label REPUBLISH_FAIL{terminal="true"} on the
            # final attempt (ADR-030 D5). The SDK's handler contract passes only the payload
            # dict, with no delivery count, so we cannot tell a first failure from the last.
            # Redelivery is still bounded by the NACK consumer's max_deliver.
            # TODO: confirm with the SDK owner whether the handler can receive a delivery
            # count/context; until then this label is "unknown", never silently "false".
            REPUBLISH_FAIL.labels("unknown").inc()
            logger.error("enricher_republish_failed", extra={"request_id": event.get("request_id")})
            raise  # withhold the ack -> redeliver

    async def stop(self) -> None:
        if self._queue is not None:
            try:
                await self._queue.close()
            except Exception:
                logger.warning("enricher_stop_failed", exc_info=True)
