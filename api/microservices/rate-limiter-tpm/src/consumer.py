"""Event-backbone consumer — the async counting strain (AD-01/AD-02, ADR-030), over the
multistack.queue SDK seam (ADR-033).

Subscribes to the gateway's **enriched** event stream (ADR-030; not the raw
`gateway.events` — see settings.py) and, for each `response` event (NOT `request`
events, unlike rate-limiter-rpm's consumer — the token count and, since AD-01, the
principal both live on the response event), reads the already-guaranteed token amount
and increments the TPM counters:

  1. `usage.total_tokens` present as a number on the event -> INCR by that amount.
     This is now the ONLY path: the enricher service (ADR-030) guarantees every enriched
     `response` event carries a normalized `usage` object, computing an estimate itself
     via the shared tokenizer when the upstream provider omitted `usage`. This service no
     longer calls the tokenizer at all (ADR-030 AD-02 supersedes ADR-011 AD-06's
     in-service fallback) — it does not distinguish `usage.source == "provider"` from
     `"estimated"`; that provenance is a billing concern, not a rate-limiting one.
  2. `usage.total_tokens` is `null`, absent, or non-numeric (i.e. the enricher itself
     found no reliable count, `usage.source == "none"`) -> skip. No count, no error, still
     acked (a poison message cannot stall the consumer).

- Durable consumer => independent cursor, survives restarts, replays on reconnect.
- Idempotent: the service dedupes on `request_id` (ADR-008), so at-least-once redelivery
  never double-counts.
- Queue I/O goes through the multistack.queue SDK, not a backend client; the SDK is imported
  lazily so the module (and offline tests) load without it. The SDK owns deserialization,
  acknowledgement and reconnection.
- The durable consumer itself is provisioned externally by NACK — this service binds to it
  and never creates streams or consumers.
- Failure-isolated: connect/consume errors are logged; they never crash the service. If the
  backbone is unreachable, counters simply don't advance and `/check` fails open (allows).
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# How long startup may wait for the backbone before giving up and handing off to the
# background retry. The SDK retries its INITIAL connect internally and indefinitely
# (NatsConfig.retry_on_initial_connect defaults to True, 1s->30s backoff) and QueueConfig
# exposes no knob to turn that off — so `await queue.connect()` on an unreachable backbone
# never returns, which would hang lifespan startup and leave the service not listening at
# all. Bounding it keeps ADR-016's promise: a down backbone leaves the consumer inactive,
# never blocks or crashes startup. _retry_connect() below owns the waiting instead.
_CONNECT_TIMEOUT_SECONDS = 10


class TpmConsumer:
    def __init__(self, settings, service, *, queue=None) -> None:
        self._url = settings.event_backbone_url
        self._backend = settings.queue_backend
        self._stream = settings.event_stream_name
        self._durable = settings.event_durable_name
        self._service = service
        self._queue = queue  # test override; production builds it in start()
        self._bound = False
        # Hold a strong ref to the background retry task: the loop keeps only a weak
        # reference, so an un-stored task can be GC'd mid-backoff (RUF006).
        self._retry_task: asyncio.Task | None = None

    @property
    def active(self) -> bool:
        """True once the durable consumer is bound (i.e. counting is live). Read by /ready
        and the consumer-active metric."""
        return self._bound

    async def start(self) -> None:
        """Best-effort connect through the multistack.queue SDK. A down backbone logs and
        leaves the consumer inactive (counts won't advance) rather than crashing startup,
        while a background task keeps retrying until it succeeds (_retry_connect). Reconnect
        after a first successful connect is the SDK's own job (its NATS client reconnects and
        restores the push subscription), so this service no longer rebinds by hand."""
        try:
            from multistack.queue import Queue, QueueConfig  # lazy: offline tests degrade

            queue = Queue(QueueConfig(name=self._backend, url=self._url))
            await asyncio.wait_for(queue.connect(), timeout=_CONNECT_TIMEOUT_SECONDS)
            self._queue = queue
        except Exception:
            logger.warning("tpm_consumer_connect_failed_retrying", exc_info=True)
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
        """Bind to the NACK-provisioned durable consumer, classifying failures. The consumer
        (its filter subject, deliver policy, max_deliver and ack_wait) is provisioned
        externally by NACK; this only binds to it and chooses runtime ack behaviour.

        AckMode.ALWAYS keeps the pre-SDK soft-ack: the message is acked even when the handler
        fails, so a poison message can never stall counting."""
        try:
            from multistack.queue.drivers.nats.messaging import AckMode, ConsumerBinding

            await self._queue.subscribe(
                ConsumerBinding(
                    stream=self._stream,
                    durable=self._durable,
                    ack_mode=AckMode.ALWAYS,
                ),
                self._on_msg,
            )
            self._bound = True
            logger.info(
                "tpm_consumer_started", extra={"stream": self._stream, "durable": self._durable}
            )
        except Exception as exc:
            self._bound = False
            text = str(exc).lower()
            if "already" in text and ("exist" in text or "bound" in text or "in use" in text):
                logger.warning(
                    "tpm_consumer_durable_conflict", exc_info=True
                )  # another replica holds it
            else:
                logger.warning(
                    "tpm_consumer_bind_failed", exc_info=True
                )  # transient — retried on reconnect

    def _resolve_amount(self, event: dict) -> int | None:
        """Read the enriched, already-guaranteed count. No tokenizer call, no text
        extraction — the enricher (ADR-030) has already done that work. `total_tokens`
        must be a real number (bool excluded: isinstance(True, int) is True in Python but
        a bool is never a legitimate token count)."""
        usage = event.get("usage")
        total = usage.get("total_tokens") if isinstance(usage, dict) else None
        if isinstance(total, (int, float)) and not isinstance(total, bool):
            return int(total)
        return None

    async def _on_msg(self, event: dict) -> None:
        """Handle one already-deserialized event. The SDK deserializes the payload and acks
        (AckMode.ALWAYS), so this neither parses nor acknowledges. Errors are swallowed and
        logged: counting must never take the service down."""
        try:
            if event.get("type") == "response":
                amount = self._resolve_amount(event)
                if amount:
                    await self._service.count(
                        event.get("principal"), event.get("model"), event.get("request_id"), amount
                    )
        except Exception:
            logger.warning("tpm_consume_error", exc_info=True)

    async def stop(self) -> None:
        if self._queue is not None:
            try:
                await self._queue.close()
            except Exception:
                logger.warning("tpm_consumer_stop_failed", exc_info=True)
