"""NATS JetStream enricher stage (ADR-030).

Consumes `response` events from the gateway's `gateway.events` (stream GATEWAY_EVENTS),
guarantees a normalized token count via `resolve_usage`, and republishes to a separate
`gateway.events.enriched` (stream GATEWAY_EVENTS_ENRICHED) with `Nats-Msg-Id = request_id`
for JetStream dedup. Only `response` events are enriched; `request`/`chunk` are skipped.

Resilience mirrors rate-limiter-tpm's consumer (ADR-016): lazy `nats` import,
`retry_on_failed_connect`, rebind on reconnect, failure-isolated, never crashes the service.

Ack policy DIVERGES from TPM's unconditional `finally: ack()` (ADR-030 D5): the enricher is
the SOLE producer of a stream billing depends on, so a dropped republish is real data loss.
Therefore it acks ONLY after a successful republish; on republish failure it does NOT ack, so
JetStream redelivers (bounded by max_deliver + AckWait). Terminal exhaustion is logged +
metered, never silent. A genuinely un-parseable message is acked so a poison message cannot
stall the consumer. Recovery after terminal failure is operator-driven replay from the retained
input stream.
"""

from __future__ import annotations

import asyncio
import json
import logging

from prometheus_client import Counter
from service import resolve_usage

logger = logging.getLogger(__name__)

# Module-level (not per-instance) so repeated create_app() in tests never double-registers.
EVENTS = Counter("enricher_events_total", "Enriched events by usage source", ["source", "reason"])
REPUBLISH_FAIL = Counter(
    "enricher_republish_failures_total", "Republish failures (terminal=true on max_deliver exhaustion)", ["terminal"]
)
TOKENIZER_FALLBACK = Counter("enricher_tokenizer_fallback_total", "Tokenizer fallback outcomes", ["outcome"])

# Fields carried through from the source `response` event onto the enriched event.
# `body` is deliberately dropped; `usage` is replaced with the normalized object.
_PRESERVED = (
    "request_id", "principal", "model", "status", "ts", "duration_ms", "stream",
    # Billing attribution (add-enricher-usage-attribution) — carried through from the gateway
    # event unchanged so downstream (billing) can attribute usage per key/user; absent on
    # pre-attribution source events, in which case .get() below yields None (never fabricated).
    "api_key_id", "owner_id",
)


class _RepublishError(Exception):
    """Raised when publishing an enriched event fails, to trigger the no-ack/redeliver path."""


class EnricherConsumer:
    def __init__(self, settings, *, http_client=None, js=None) -> None:
        self._url = settings.event_backbone_url
        self._in_stream = settings.event_stream_name
        self._in_subject = settings.event_stream_subject
        self._durable = settings.event_durable_name
        self._out_stream = settings.enriched_stream_name
        self._out_subject = settings.enriched_subject
        self._max_deliver = settings.max_deliver
        self._ack_wait = settings.ack_wait_seconds
        self._tokenizer_url = settings.tokenizer_url
        self._tokenizer_timeout_ms = settings.tokenizer_timeout_ms
        self._http_client = http_client
        self._js = js  # test override; production derives it from the live connection
        self._nc = None
        self._sub = None

    @property
    def active(self) -> bool:
        """True once the JetStream subscription is bound (counting/republishing is live)."""
        return self._sub is not None

    async def start(self) -> None:
        """Best-effort connect (mirrors rate-limiter-tpm). A down backbone leaves the consumer
        inactive rather than crashing startup, while a background task keeps retrying until
        it succeeds — see _retry_connect().

        FIXED: this used to pass retry_on_failed_connect=True to nats.connect() expecting it
        to retry a down-at-startup backbone in the background. That parameter has never
        existed in nats-py, at any version (confirmed by inspecting Client.connect()'s actual
        signature and by searching nats.py's full commit history) — so connect() always
        raised immediately instead, and this consumer never bound, ever, unless NATS happened
        to be reachable at the exact moment start() ran. Native reconnect (max_reconnect_
        attempts=-1) only takes over after a first successful connect, so it never engaged
        either. _retry_connect() below is what actually covers the down-at-startup case."""
        try:
            import nats  # lazy: offline tests / inert path don't need nats-py

            self._nc = await nats.connect(
                self._url,
                reconnected_cb=self._on_reconnected,
                disconnected_cb=self._on_disconnected,
                max_reconnect_attempts=-1,
            )
        except Exception:
            logger.warning("enricher_connect_failed_retrying", exc_info=True)
            self._nc = None
            asyncio.create_task(self._retry_connect())
            return
        if getattr(self._nc, "is_connected", False):
            await self._bind()

    async def _retry_connect(self) -> None:
        """Background retry for a backbone that was down when start() ran. Capped exponential
        backoff (1s, 2s, 4s, ... up to 30s) until connect() succeeds, then binds directly."""
        import nats

        delay = 1
        while self._nc is None:
            await asyncio.sleep(delay)
            try:
                self._nc = await nats.connect(
                    self._url,
                    reconnected_cb=self._on_reconnected,
                    disconnected_cb=self._on_disconnected,
                    max_reconnect_attempts=-1,
                )
            except Exception:
                delay = min(delay * 2, 30)
        await self._bind()

    async def _bind(self) -> None:
        """Single bind path (used by start() and the reconnect handler): create both streams
        (idempotent) and subscribe the durable consumer, classifying failures."""
        from nats.js.api import ConsumerConfig, DeliverPolicy

        js = self._nc.jetstream()
        self._js = js
        for name, subject in ((self._in_stream, self._in_subject), (self._out_stream, self._out_subject)):
            try:  # both add_stream calls are idempotent (the gateway also creates the input stream)
                await js.add_stream(name=name, subjects=[subject])
            except Exception:
                logger.info("enricher_stream_exists", extra={"stream": name})
        try:
            # DeliverPolicy.NEW: enrich events going forward from deploy (no historical backfill;
            # downstream consumers meter from the enriched stream). max_deliver/ack_wait bound the
            # not-ack-on-failure redelivery path (D5).
            self._sub = await js.subscribe(
                self._in_subject,
                durable=self._durable,
                cb=self._on_msg,
                manual_ack=True,
                config=ConsumerConfig(
                    deliver_policy=DeliverPolicy.NEW, max_deliver=self._max_deliver, ack_wait=self._ack_wait
                ),
            )
            logger.info("enricher_started", extra={"subject": self._in_subject, "durable": self._durable})
        except Exception as exc:
            self._sub = None
            text = str(exc).lower()
            if "already" in text and ("exist" in text or "bound" in text or "in use" in text):
                logger.warning("enricher_durable_conflict", exc_info=True)  # another replica holds it
            else:
                logger.warning("enricher_bind_failed", exc_info=True)  # transient — retry on reconnect

    async def _on_reconnected(self) -> None:
        if self._sub is not None:
            try:
                await self._sub.unsubscribe()
            except Exception:
                logger.warning("enricher_resubscribe_drain_failed", exc_info=True)
            self._sub = None
        await self._bind()

    async def _on_disconnected(self) -> None:
        self._sub = None
        logger.warning("enricher_disconnected")

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
            await self._js.publish(
                self._out_subject,
                json.dumps(enriched, default=str).encode(),
                headers={"Nats-Msg-Id": str(enriched["request_id"])},
            )
        except Exception as exc:
            raise _RepublishError() from exc

    @staticmethod
    def _num_delivered(msg) -> int | None:
        try:  # msg.metadata parses msg.reply and can raise on a non-JS/malformed message
            return msg.metadata.num_delivered
        except Exception:
            return None

    async def _on_msg(self, msg) -> None:
        try:
            event = json.loads(msg.data)
        except Exception:
            logger.warning("enricher_unparseable_acked", exc_info=True)
            await self._safe_ack(msg)
            return

        if not (isinstance(event, dict) and event.get("type") == "response"):
            await self._safe_ack(msg)  # request/chunk/other: skip + ack, nothing to republish
            return

        try:
            enriched = await self._build_enriched(event)
        except Exception:
            logger.warning("enricher_build_error_acked", exc_info=True)
            await self._safe_ack(msg)
            return

        try:
            await self._publish_enriched(enriched)
        except _RepublishError:
            num = self._num_delivered(msg)
            terminal = num is not None and num >= self._max_deliver
            REPUBLISH_FAIL.labels("true" if terminal else "false").inc()
            if terminal:
                logger.error("enricher_republish_terminal", extra={"request_id": event.get("request_id")})
            return  # do NOT ack -> JetStream redelivers (bounded by max_deliver)

        await self._safe_ack(msg)

    async def _safe_ack(self, msg) -> None:
        try:
            await msg.ack()
        except Exception:
            logger.warning("enricher_ack_failed", exc_info=True)

    async def stop(self) -> None:
        if self._nc is not None:
            try:
                await self._nc.drain()
            except Exception:
                logger.warning("enricher_stop_failed", exc_info=True)
