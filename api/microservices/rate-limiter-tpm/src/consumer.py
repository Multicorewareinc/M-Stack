"""NATS JetStream consumer — the async counting strain (AD-01/AD-02, ADR-030).

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
- `nats` is imported lazily so the module (and offline tests) load without nats-py.
- Failure-isolated: connect/consume errors are logged; they never crash the service. If the
  backbone is unreachable, counters simply don't advance and `/check` fails open (allows).
"""

from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger(__name__)


class TpmConsumer:
    def __init__(self, settings, service) -> None:
        self._url = settings.event_backbone_url
        self._stream = settings.event_stream_name
        self._subject = settings.event_stream_subject
        self._durable = settings.event_durable_name
        self._service = service
        self._nc = None
        self._sub = None

    @property
    def active(self) -> bool:
        """True once the JetStream subscription is bound (i.e. counting is live). Read by
        /ready and the consumer-active metric — the observable counterpart to the state
        start() already tracked but never exposed."""
        return self._sub is not None

    async def start(self) -> None:
        """Best-effort connect. A down backbone logs and leaves the consumer inactive
        (counts won't advance) rather than crashing startup, while a background task keeps
        retrying until it succeeds — see _retry_connect().

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
            logger.warning("tpm_consumer_connect_failed_retrying", exc_info=True)
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
        try:  # the gateway also creates this; add_stream is idempotent
            await js.add_stream(name=self._stream, subjects=[self._subject])
        except Exception:
            logger.info("tpm_stream_exists", extra={"stream": self._stream})
        try:
            # DeliverPolicy.NEW: count events going FORWARD only. A rolling 60s window must
            # not replay the whole retained backlog into the current minute (that would
            # over-count on every restart). Once durable, it resumes from the last ack.
            self._sub = await js.subscribe(
                self._subject,
                durable=self._durable,
                cb=self._on_msg,
                manual_ack=True,
                config=ConsumerConfig(deliver_policy=DeliverPolicy.NEW),
            )
            logger.info("tpm_consumer_started", extra={"subject": self._subject, "durable": self._durable})
        except Exception as exc:
            self._sub = None
            text = str(exc).lower()
            if "already" in text and ("exist" in text or "bound" in text or "in use" in text):
                logger.warning("tpm_consumer_durable_conflict", exc_info=True)  # another replica holds it
            else:
                logger.warning("tpm_consumer_bind_failed", exc_info=True)  # transient — will retry on reconnect

    async def _on_reconnected(self) -> None:
        # nats-py restores core subscriptions on reconnect, so drain any live sub first to
        # avoid a duplicate JS push binding, then rebind. Re-binding cannot double-count: the
        # durable resumes from its last ack and counting is idempotent on request_id.
        if self._sub is not None:
            try:
                await self._sub.unsubscribe()
            except Exception:
                logger.warning("tpm_resubscribe_drain_failed", exc_info=True)
            self._sub = None
        await self._bind()

    async def _on_disconnected(self) -> None:
        # Reflect the outage in `active`/ /ready until the connection is restored.
        self._sub = None
        logger.warning("tpm_consumer_disconnected")

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

    async def _on_msg(self, msg) -> None:
        try:
            event = json.loads(msg.data)
            if isinstance(event, dict) and event.get("type") == "response":
                amount = self._resolve_amount(event)
                if amount:
                    await self._service.count(
                        event.get("principal"), event.get("model"), event.get("request_id"), amount
                    )
        except Exception:
            logger.warning("tpm_consume_error", exc_info=True)
        finally:
            try:
                await msg.ack()  # soft: ack even on error so a poison msg can't loop forever
            except Exception:
                logger.warning("tpm_ack_failed", exc_info=True)

    async def stop(self) -> None:
        if self._nc is not None:
            try:
                await self._nc.drain()
            except Exception:
                logger.warning("tpm_consumer_stop_failed", exc_info=True)
