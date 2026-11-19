"""Event-backbone consumer — billing's metering strain (ADR-031), over the multistack.queue
SDK seam (ADR-033).

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

Queue I/O goes through the multistack.queue SDK (ADR-033), not a backend client: the SDK is
imported lazily (offline tests degrade gracefully) and owns deserialization, acknowledgement
and reconnection. The durable consumer is provisioned externally by NACK — this service binds
to it and never creates streams or consumers. Under AckMode.ON_SUCCESS a handler that returns
is acked and a handler that RAISES is redelivered, which is how the no-ack-on-genuine-fault
rule above is now expressed.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from models import Outbox, Usage
from prometheus_client import Counter, Gauge
from sqlalchemy.exc import IntegrityError

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


class _PersistError(Exception):
    """Raised when persisting a usage event genuinely fails, to withhold the ack.

    Under AckMode.ON_SUCCESS the SDK acks only when the handler returns, so raising is how
    this consumer asks for redelivery — the successor to the old "do not call msg.ack()"
    path (ADR-031 D4). Deliberately NOT raised for an already-processed request_id, which is
    a no-op, not a failure."""


class BillingConsumer:
    def __init__(self, settings, sessionmaker, *, queue=None) -> None:
        self._url = settings.event_backbone_url
        self._backend = settings.queue_backend
        self._stream = settings.event_stream_name
        self._durable = settings.event_durable_name
        self._sessionmaker = sessionmaker
        self._queue = queue  # test override; production builds it in start()
        self._bound = False
        # Hold a strong ref to the background retry task: the loop keeps only a weak
        # reference, so an un-stored task can be GC'd mid-backoff (RUF006).
        self._retry_task: asyncio.Task | None = None

    @property
    def active(self) -> bool:
        return self._bound

    async def start(self) -> None:
        """Best-effort connect. A down backbone leaves the consumer inactive (metering paused,
        never silently dropped — messages simply queue on the retained stream) rather than
        crashing startup, while a background task keeps retrying until it succeeds — see
        _retry_connect(). Reconnect after a first successful connect is the SDK's own job."""
        try:
            from multistack.queue import Queue, QueueConfig  # lazy: offline tests degrade

            queue = Queue(QueueConfig(name=self._backend, url=self._url))
            await asyncio.wait_for(queue.connect(), timeout=_CONNECT_TIMEOUT_SECONDS)
            self._queue = queue
        except Exception:
            logger.warning("billing_consumer_connect_failed_retrying", exc_info=True)
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

        AckMode.ON_SUCCESS is what preserves ADR-031's metering guarantee: the SDK acks only
        when the handler returns, so a handler that RAISES leaves the message unacknowledged
        and the backbone redelivers it. That is how a genuine persistence fault avoids
        silently dropping billable usage."""
        try:
            from multistack.queue.drivers.nats.messaging import AckMode, ConsumerBinding

            await self._queue.subscribe(
                ConsumerBinding(
                    stream=self._stream,
                    durable=self._durable,
                    ack_mode=AckMode.ON_SUCCESS,
                ),
                self._on_msg,
            )
            self._bound = True
            logger.info(
                "billing_consumer_started", extra={"stream": self._stream, "durable": self._durable}
            )
        except Exception as exc:
            self._bound = False
            text = str(exc).lower()
            if "already" in text and ("exist" in text or "bound" in text or "in use" in text):
                logger.warning(
                    "billing_consumer_durable_conflict", exc_info=True
                )  # another replica holds it
            else:
                logger.warning("billing_consumer_bind_failed", exc_info=True)

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
                completion_tokens=(
                    usage.get("completion_tokens") if isinstance(usage, dict) else None
                ),
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

    async def _on_msg(self, event: dict) -> None:
        """Handle one already-deserialized enriched event.

        The SDK owns deserialization (an unparseable payload is logged and acked by the
        driver before reaching here — redelivery cannot fix malformed JSON) and
        acknowledgement. Under AckMode.ON_SUCCESS the contract is:

          - return normally  -> the SDK acks (fresh success, or a confirmed already-processed
                                request_id no-op, which is a no-op and not a failure);
          - RAISE            -> the SDK withholds the ack, so the backbone redelivers once the
                                fault clears (ADR-031 D4). This is the inversion of the old
                                "don't call msg.ack()" path.
        """
        if event.get("type") != "response":
            return  # non-response: skip, nothing to persist (acked)

        if not await self._persist(event):
            # A genuine fault (not a request_id no-op). Raise so the message is NOT acked.
            raise _PersistError(
                f"billing persist failed for request_id={event.get('request_id')!r} "
                "-- withholding ack for redelivery"
            )

    async def stop(self) -> None:
        if self._queue is not None:
            try:
                await self._queue.close()
            except Exception:
                logger.warning("billing_consumer_stop_failed", exc_info=True)
