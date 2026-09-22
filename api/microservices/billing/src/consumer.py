"""NATS JetStream consumer — billing's metering strain (ADR-031).

Durable consumer of `gateway.events.enriched` (stream GATEWAY_EVENTS_ENRICHED, produced by
the `enricher` service, ADR-030). For each `response` event it opens a FRESH AsyncSession
(via the injected sessionmaker — NOT the request-scoped `get_session` FastAPI dependency,
which has no meaning outside an HTTP request) and, in one transaction:

  - countable (usage.total_tokens numeric) -> a `usage` row + a `pending` `outbox` row.
  - un-countable (usage.source == "none", or a malformed/non-numeric total_tokens the
    enricher's own contract should never actually produce) -> a `usage` row only, audited,
    never billed on a guess.

Idempotency is a Postgres UNIQUE(request_id) constraint, not an app-level dedupe store
(ADR-031 AD-04): a redelivered event's insert raises IntegrityError, which is treated as an
already-processed no-op ONLY when it is specifically the request_id constraint — any other
integrity failure (or any commit-time fault) is a genuine error and does NOT ack, so
JetStream redelivers rather than silently swallowing a data problem as harmless redelivery.

Resilience mirrors rate-limiter-tpm/enricher (ADR-016): lazy `nats` import,
`retry_on_failed_connect`, rebind on reconnect, failure-isolated.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

from models import Outbox, Usage
from prometheus_client import Counter, Gauge
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)

# Module-level (not per-instance) so repeated create_app() in tests never double-registers.
USAGE_PERSISTED = Counter(
    "billing_usage_persisted_total", "Usage rows persisted by countability", ["countable"]
)
CONSUMER_ACTIVE = Gauge("billing_consumer_active", "1 when the billing consumer is bound, else 0")

# Markers for the UNIQUE(request_id) constraint's error text, present in both aiosqlite's
# ("UNIQUE constraint failed: usage.request_id") and Postgres/asyncpg's ('duplicate key value
# violates unique constraint "uq_usage_request_id"') text for a violation of it — one
# driver-portable check, not a per-driver branch. BOTH markers are required: "request_id"
# alone is not enough — a NOT NULL violation on the same column ("NOT NULL constraint
# failed: usage.request_id" / 'null value in column "request_id" violates not-null
# constraint') also contains that substring, and must NOT be swallowed as a harmless
# redelivery no-op.
_UNIQUE_MARKER = "unique"
_REQUEST_ID_CONSTRAINT_MARKER = "request_id"


def _is_numeric_not_bool(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_request_id_violation(exc: IntegrityError) -> bool:
    """True only for the usage.request_id UNIQUE violation specifically — IntegrityError is
    SQLAlchemy's broad wrapper for UNIQUE/NOT NULL/CHECK/FK failures alike; treating any of
    them as "already processed" would silently swallow a genuine data bug. Requires BOTH
    markers so a NOT NULL violation on the same column (which also contains "request_id" in
    its error text) is correctly classified as a genuine fault, not a no-op."""
    text = str(getattr(exc, "orig", exc)).lower()
    return _UNIQUE_MARKER in text and _REQUEST_ID_CONSTRAINT_MARKER in text


class BillingConsumer:
    def __init__(self, settings, sessionmaker) -> None:
        self._url = settings.event_backbone_url
        self._stream = settings.event_stream_name
        self._subject = settings.event_stream_subject
        self._durable = settings.event_durable_name
        self._sessionmaker = sessionmaker
        self._nc = None
        self._sub = None

    @property
    def active(self) -> bool:
        return self._sub is not None

    async def start(self) -> None:
        """Best-effort connect. A down backbone leaves the consumer inactive (metering paused,
        never silently dropped — messages simply queue on the retained stream) rather than
        crashing startup, while a background task keeps retrying until it succeeds — see
        _retry_connect().

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
            logger.warning("billing_consumer_connect_failed_retrying", exc_info=True)
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
        """Single bind path (used by start() and the reconnect handler): create the stream
        (idempotent) and subscribe the durable consumer, classifying failures."""
        from nats.js.api import ConsumerConfig, DeliverPolicy

        js = self._nc.jetstream()
        try:  # the enricher also creates this; add_stream is idempotent
            await js.add_stream(name=self._stream, subjects=[self._subject])
        except Exception:
            logger.info("billing_stream_exists", extra={"stream": self._stream})
        try:
            self._sub = await js.subscribe(
                self._subject,
                durable=self._durable,
                cb=self._on_msg,
                manual_ack=True,
                config=ConsumerConfig(deliver_policy=DeliverPolicy.NEW),
            )
            logger.info("billing_consumer_started", extra={"subject": self._subject, "durable": self._durable})
        except Exception as exc:
            self._sub = None
            text = str(exc).lower()
            if "already" in text and ("exist" in text or "bound" in text or "in use" in text):
                logger.warning("billing_consumer_durable_conflict", exc_info=True)  # another replica holds it
            else:
                logger.warning("billing_consumer_bind_failed", exc_info=True)

    async def _on_reconnected(self) -> None:
        if self._sub is not None:
            try:
                await self._sub.unsubscribe()
            except Exception:
                logger.warning("billing_resubscribe_drain_failed", exc_info=True)
            self._sub = None
        await self._bind()

    async def _on_disconnected(self) -> None:
        self._sub = None
        logger.warning("billing_consumer_disconnected")

    async def _persist(self, event: dict) -> bool:
        """Write the usage (+ outbox, if countable) row(s) for one event. Returns True if the
        message should be acked (fresh success, or a confirmed request_id no-op), False if a
        genuine fault occurred and the message must NOT be acked (JetStream redelivers)."""
        usage = event.get("usage")
        total = usage.get("total_tokens") if isinstance(usage, dict) else None
        source = (usage.get("source") if isinstance(usage, dict) else None) or "none"
        reason = usage.get("reason") if isinstance(usage, dict) else None
        countable = _is_numeric_not_bool(total)

        ts_raw = event.get("ts")
        ts = datetime.fromtimestamp(ts_raw, UTC) if _is_numeric_not_bool(ts_raw) else None

        session = self._sessionmaker()
        try:
            usage_row = Usage(
                request_id=event.get("request_id"),
                principal=event.get("principal"),
                # Per-request attribution (add-billing-usage-attribution) — best-effort, same as
                # principal/model; None when the enriched event carried none. NOT added to the
                # outbox payload below: the Stripe reporter meters per-org (principal) only.
                api_key_id=event.get("api_key_id"),
                owner_id=event.get("owner_id"),
                model=event.get("model"),
                total_tokens=int(total) if countable else None,
                source=source,
                reason=reason,
                prompt_tokens=usage.get("prompt_tokens") if isinstance(usage, dict) else None,
                completion_tokens=usage.get("completion_tokens") if isinstance(usage, dict) else None,
                ts=ts,
            )
            session.add(usage_row)
            if countable:
                session.add(
                    Outbox(
                        request_id=event.get("request_id"),
                        payload={
                            "request_id": event.get("request_id"),
                            "principal": event.get("principal"),
                            "model": event.get("model"),
                            "total_tokens": int(total),
                            "source": source,
                            "ts": ts_raw,
                        },
                        status="pending",
                    )
                )

            try:
                await session.flush()
            except IntegrityError as exc:
                await session.rollback()
                if _is_request_id_violation(exc):
                    return True  # already processed — a no-op, not a failure
                logger.warning("billing_integrity_error_not_redelivery", exc_info=True)
                return False  # a genuine, different integrity fault — do not swallow it

            await session.commit()
            USAGE_PERSISTED.labels(countable="true" if countable else "false").inc()
            return True
        except Exception:
            await session.rollback()
            logger.warning("billing_persist_failed", exc_info=True)
            return False
        finally:
            await session.close()

    async def _on_msg(self, msg) -> None:
        try:
            event = json.loads(msg.data)
        except Exception:
            logger.warning("billing_unparseable_acked", exc_info=True)
            await self._safe_ack(msg)
            return

        if not (isinstance(event, dict) and event.get("type") == "response"):
            await self._safe_ack(msg)  # non-response: skip, nothing to persist
            return

        ok = await self._persist(event)
        if ok:
            await self._safe_ack(msg)
        # else: do NOT ack — JetStream redelivers once the fault clears (D4)

    async def _safe_ack(self, msg) -> None:
        try:
            await msg.ack()
        except Exception:
            logger.warning("billing_ack_failed", exc_info=True)

    async def stop(self) -> None:
        if self._nc is not None:
            try:
                await self._nc.drain()
            except Exception:
                logger.warning("billing_consumer_stop_failed", exc_info=True)
