"""Stripe reporter — the outbox-draining strain (ADR-031 AD-05/AD-06), a second surface of
this same service. Polls `outbox` for `pending` rows due for an attempt (no event to react
to — rows are written by a separate process, BillingConsumer, so a poll is the minimal
correct mechanism) and pushes each to Stripe individually, per-event.

Outbox lifecycle:
  pending -> sent               (successful Stripe call; terminal, never re-attempted)
  pending -> pending (backoff)  (transient failure, attempts below the ceiling)
  pending -> failed             (transient failure at the attempt ceiling, OR any
                                  permanent failure on its first attempt; terminal, but the
                                  row is never deleted — durable for ops follow-up. Resetting
                                  a failed row to pending for manual retry must also reset
                                  `attempts` to 0.)

Idempotency: every Stripe call carries an idempotency key — `billing:{request_id}` for a usage
report, `billing:customer:{principal}` for a Stripe Customer creation (stable per-org, not
per-event) — so neither this reporter's own retry nor a Stripe-side network retry can
double-report or double-create.

Single-instance assumption: the poll query has no row-claiming step (no `SELECT ... FOR
UPDATE SKIP LOCKED`). Stripe-side double-reporting is still prevented by the idempotency key
(derived purely from request_id, independent of instance count), but concurrent reporter
instances could race on the same row's local status/attempts bookkeeping. Upgrade path if
`billing` ever scales horizontally: `FOR UPDATE SKIP LOCKED` or a pending->in_flight claim.

Customer mapping (ADR-031 AD-06, resolved): before reporting a row, `_process_row` resolves
`principal` (org id) to a real Stripe Customer via `create_or_get_stripe_customer` — creating
one lazily on first need and reusing it forever after (`stripe_customers`, `UNIQUE(principal)`).
The Meter Event's customer-identifying field carries the RESOLVED `stripe_customer_id`, not the
raw `principal` — a "sent" outcome now proves correct billing attribution, not merely delivery.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Literal

from models import Outbox, StripeCustomer
from prometheus_client import Counter, Gauge
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

ReportResult = Literal["sent", "transient_failure", "permanent_failure"]
CustomerResult = Literal["created", "reused", "transient_failure", "permanent_failure"]

# Module-level (not per-instance) so repeated create_app() in tests never double-registers.
REPORTED = Counter(
    "billing_stripe_reported_total", "Outbox rows reported to Stripe by outcome",
    ["outcome"],  # sent | transient_failure | terminal_failure
)
# Exactly 3 labels, deliberately NOT mirroring REPORTED's transient/terminal split (design D6):
# a transient (retryable, non-ceiling) customer-creation failure is not yet a terminal outcome
# and increments none of these — it is observable via the outbox row's own attempts field
# instead. "failed" here means permanent_failure only.
CUSTOMER_RESOLVED = Counter(
    "billing_stripe_customer_resolved_total", "Stripe customer resolution outcomes",
    ["outcome"],  # created | reused | failed
)
REPORTER_ACTIVE = Gauge("billing_reporter_active", "1 when the reporter's poll task is running, else 0")


async def report_to_stripe(
    client, *, event_name: str, principal: str | None, total_tokens: int, idempotency_key: str,
    customer_field: str = "stripe_customer_id",
) -> ReportResult:
    """Push one usage event to Stripe as a Billing Meter Event. Never raises — the only
    place that touches the `stripe` SDK. Catches the TOP-LEVEL exception names (not
    `stripe.error.*`, which SDK v15 removed) in an order where subclass relationships matter:
    `RateLimitError` is a SUBCLASS of `InvalidRequestError` in stripe-python's hierarchy, so it
    MUST be checked before `InvalidRequestError` or a 429 would be misclassified as permanent."""
    import stripe  # lazy: offline tests / the inert path never need this resolvable

    try:
        client.billing.meter_events.create(
            {
                "event_name": event_name,
                "payload": {"value": str(total_tokens), customer_field: principal},
            },
            options={"idempotency_key": idempotency_key},
        )
        return "sent"
    except stripe.APIConnectionError:
        logger.warning("stripe_report_transient_connection", exc_info=True)
        return "transient_failure"
    except stripe.RateLimitError:  # MUST precede InvalidRequestError (subclass ordering)
        logger.warning("stripe_report_transient_rate_limited", exc_info=True)
        return "transient_failure"
    except stripe.APIError:
        logger.warning("stripe_report_transient_api_error", exc_info=True)
        return "transient_failure"
    except stripe.InvalidRequestError:
        logger.warning("stripe_report_permanent_invalid_request", exc_info=True)
        return "permanent_failure"
    except stripe.StripeError:  # catch-all (e.g. AuthenticationError) — retry cannot help
        logger.warning("stripe_report_permanent_other", exc_info=True)
        return "permanent_failure"


async def create_or_get_stripe_customer(
    client, session: AsyncSession, *, principal: str,
) -> tuple[CustomerResult, str | None]:
    """Resolve `principal` (org id) to a real Stripe Customer, creating one on first need and
    reusing it forever after (ADR-031 AD-06). Takes the CALLER'S already-open `session` (design
    D2) — never opens or commits its own; a successful create is `session.add()`-ed but left for
    the caller's own trailing commit. Never raises — same top-level-exception classification as
    `report_to_stripe` (copied, not re-derived, so the two functions stay consistent by
    construction). Returns the resolved `stripe_customer_id` directly alongside the outcome so
    the caller never needs a second query to learn what this call itself just resolved."""
    existing = (
        await session.execute(select(StripeCustomer).where(StripeCustomer.principal == principal))
    ).scalar_one_or_none()
    if existing is not None:
        return "reused", existing.stripe_customer_id

    import stripe  # lazy: offline tests / the inert path never need this resolvable

    try:
        customer = client.customers.create(
            {"metadata": {"org_id": principal}},
            options={"idempotency_key": f"billing:customer:{principal}"},
        )
    except stripe.APIConnectionError:
        logger.warning("stripe_customer_create_transient_connection", exc_info=True)
        return "transient_failure", None
    except stripe.RateLimitError:  # MUST precede InvalidRequestError (subclass ordering)
        logger.warning("stripe_customer_create_transient_rate_limited", exc_info=True)
        return "transient_failure", None
    except stripe.APIError:
        logger.warning("stripe_customer_create_transient_api_error", exc_info=True)
        return "transient_failure", None
    except stripe.InvalidRequestError:
        logger.warning("stripe_customer_create_permanent_invalid_request", exc_info=True)
        return "permanent_failure", None
    except stripe.StripeError:  # catch-all (e.g. AuthenticationError) — retry cannot help
        logger.warning("stripe_customer_create_permanent_other", exc_info=True)
        return "permanent_failure", None

    session.add(StripeCustomer(principal=principal, stripe_customer_id=customer.id))
    return "created", customer.id


def _backoff_seconds(attempts: int, *, base: int, cap: int) -> int:
    return min(base * (2 ** (attempts - 1)), cap)


class StripeReporter:
    def __init__(
        self, settings, sessionmaker, *,
        stripe_client=None, report_fn=None, customer_resolver=None,
    ) -> None:
        self._settings = settings
        self._sessionmaker = sessionmaker
        self._stripe_client = stripe_client
        if self._stripe_client is None and settings.stripe_secret_key:
            import stripe  # lazy: inert path / offline tests without a real key don't need this

            self._stripe_client = stripe.StripeClient(api_key=settings.stripe_secret_key)
        # Injectable independently of `stripe_client` (two-layer test seam, design D7): layer-1
        # tests call `report_to_stripe`/`create_or_get_stripe_customer` directly to exercise
        # their real exception classification; layer-2 (lifecycle) tests inject a fake
        # `report_fn`/`customer_resolver` here that returns a result directly, bypassing
        # classification entirely, so `run_once`'s outbox-lifecycle handling (status/attempts/
        # next_attempt_at transitions) is tested independently of it.
        self._report_fn = report_fn if report_fn is not None else report_to_stripe
        self._customer_resolver = (
            customer_resolver if customer_resolver is not None else create_or_get_stripe_customer
        )
        self._task: asyncio.Task | None = None

    @property
    def stripe_client(self):
        """Read-only access to the (possibly None, when inert) constructed Stripe client — lets
        the internal subscription endpoint (add-billing-plan-subscription-linkage) reuse the
        SAME client rather than constructing a second one, without reaching into the private
        `_stripe_client` attribute from outside this class."""
        return self._stripe_client

    def _apply_transient_failure(self, row: Outbox, *, already_incremented: bool = False) -> None:
        """Shared bookkeeping for a transient failure at ANY step of processing this row
        (customer resolution or usage reporting) — identical attempts/backoff/terminal-failure
        semantics either way (design D3: no second failure-tracking mechanism on Outbox).
        `already_incremented` covers the usage-reporting call site, which bumps `attempts`
        itself before calling Stripe (for idempotency-key purposes) — this avoids double-
        counting a single attempt as two."""
        if not already_incremented:
            row.attempts += 1
        if row.attempts >= self._settings.reporter_max_attempts:
            row.status = "failed"
            logger.error("billing_stripe_report_terminal", extra={"request_id": row.request_id})
            REPORTED.labels(outcome="terminal_failure").inc()
        else:
            delay = _backoff_seconds(
                row.attempts,
                base=self._settings.reporter_backoff_base_seconds,
                cap=self._settings.reporter_backoff_max_seconds,
            )
            row.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
            REPORTED.labels(outcome="transient_failure").inc()

    def _apply_permanent_failure(self, row: Outbox) -> None:
        """Shared bookkeeping for a permanent (non-retryable) failure at ANY step — terminal on
        the first attempt, `attempts` NOT incremented (design D3)."""
        row.status = "failed"
        logger.error("billing_stripe_report_terminal", extra={"request_id": row.request_id})
        REPORTED.labels(outcome="terminal_failure").inc()

    async def run_once(self) -> None:
        """One poll cycle. Inert (no query, no action) when no Stripe client is configured —
        this is what makes the inert-by-default gate real: zero calls, zero mutations."""
        if self._stripe_client is None:
            return

        s = self._settings
        async with self._sessionmaker() as session:
            now = datetime.now(UTC)
            rows = list(
                await session.scalars(
                    select(Outbox)
                    .where(Outbox.status == "pending")
                    .where((Outbox.next_attempt_at.is_(None)) | (Outbox.next_attempt_at <= now))
                    .order_by(Outbox.created_at)
                    .limit(s.reporter_batch_size)
                )
            )

        for row in rows:
            await self._process_row(row.id)

    async def _process_row(self, row_id) -> None:
        async with self._sessionmaker() as session:
            row = await session.get(Outbox, row_id)
            if row is None or row.status != "pending":
                return  # already handled (e.g. by a concurrent poller — best-effort guard)

            # Customer resolution (design D2/D3, add-billing-stripe-customer-mapping): resolve
            # BEFORE reporting, in this SAME session — never a second, independently-committed
            # one. A transient/permanent failure here stops before report_fn is ever called for
            # this row, applying the row's own attempts/backoff/terminal-failure bookkeeping (the
            # same helpers a usage-report failure uses below) so the single trailing commit below
            # persists it — no early return, or that bookkeeping would silently never persist.
            customer_outcome, stripe_customer_id = await self._customer_resolver(
                self._stripe_client, session, principal=row.payload.get("principal"),
            )
            if customer_outcome == "created":
                CUSTOMER_RESOLVED.labels(outcome="created").inc()
            elif customer_outcome == "reused":
                CUSTOMER_RESOLVED.labels(outcome="reused").inc()
            elif customer_outcome == "transient_failure":
                self._apply_transient_failure(row)
                await session.commit()
                return
            else:  # permanent_failure
                CUSTOMER_RESOLVED.labels(outcome="failed").inc()
                self._apply_permanent_failure(row)
                await session.commit()
                return

            # `attempts` is bumped here, before the call, rather than only in the failure
            # handler — that way every actual attempt (success, transient failure, or permanent
            # failure) gets its own idempotency key instead of some attempts sharing one.
            row.attempts += 1
            result = await self._report_fn(
                self._stripe_client,
                event_name=self._settings.stripe_meter_event_name,
                principal=stripe_customer_id,
                total_tokens=row.payload.get("total_tokens"),
                idempotency_key=f"billing:{row.request_id}:{row.attempts}",
                customer_field=self._settings.stripe_meter_customer_field,
            )

            if result == "sent":
                row.status = "sent"
                row.sent_at = datetime.now(UTC)
                REPORTED.labels(outcome="sent").inc()
            elif result == "transient_failure":
                self._apply_transient_failure(row, already_incremented=True)
            else:  # permanent_failure — terminal on the first attempt, no retry scheduled
                self._apply_permanent_failure(row)

            await session.commit()

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.warning("billing_reporter_cycle_failed", exc_info=True)
            await asyncio.sleep(self._settings.reporter_poll_interval_seconds)

    async def start(self) -> None:
        self._task = asyncio.create_task(self.run_forever())
        REPORTER_ACTIVE.set(1.0)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        REPORTER_ACTIVE.set(0.0)
