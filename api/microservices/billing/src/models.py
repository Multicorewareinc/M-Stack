"""ORM models: the usage ledger and its transactional outbox (ADR-031). Portable column types
only (Uuid/String/Integer/DateTime(timezone=True) with Python-side defaults), so this runs
unchanged on both aiosqlite (offline tests) and Postgres (prod) — ADR-015a, mirroring
admin-control-plane/src/modules/plans/models.py's discipline."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from db import Base
from sqlalchemy import JSON, DateTime, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class Usage(Base):
    """One row per enriched response event this service has processed (countable or not).
    `request_id` UNIQUE is the idempotency guarantee (ADR-031 AD-04): a redelivered event's
    insert violates this constraint and is treated as an already-processed no-op."""

    __tablename__ = "usage"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    request_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    principal: Mapped[str | None] = mapped_column(String, nullable=True)
    # Per-request attribution (add-billing-usage-attribution) — FK-less opaque ids from Org CP's
    # api_keys/users tables, same no-cross-service-FK rationale as `principal` (database-per-
    # service, ADR-003/006). NULL when the enriched event carried no attribution (pre-attribution
    # history or replay) — never fabricated.
    api_key_id: Mapped[str | None] = mapped_column(String, nullable=True)
    owner_id: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    # NULL when the event was un-countable (usage.source == "none") — audited, never billed.
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    # Provider token breakdown (ADR-030 D6), persisted additively when present.
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The source event's own timestamp (converted from its raw Unix-epoch float, D7).
    ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class Outbox(Base):
    """One pending row per COUNTABLE metered event, written in the same transaction as its
    `Usage` row (ADR-031 AD-04/AD-05). `payload` carries everything the (separate, later)
    Stripe reporter needs so it never joins back to `usage` (design D5). `attempts`/
    `next_attempt_at`/`sent_at` are provisioned for that reporter's lifecycle; unused by this
    service's own write path."""

    __tablename__ = "outbox"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # FK-less by design: the usage<->outbox pairing is transactional (D3), not referential.
    request_id: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StripeCustomer(Base):
    """Maps an org (`principal`) to a real Stripe Customer (ADR-031 AD-06). `UNIQUE(principal)`
    is what makes customer creation idempotent at the DB level: at most one Stripe Customer is
    ever created per org, looked up before every report. Created lazily by the Stripe reporter
    on first need for a given org (add-billing-stripe-customer-mapping), never by the metering
    consumer."""

    __tablename__ = "stripe_customers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    principal: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    stripe_customer_id: Mapped[str] = mapped_column(String, nullable=False)
    # Subscription linkage (add-billing-plan-subscription-linkage, ADR-031 AD-06 part 2) — a
    # Stripe Subscription is 1:1 with a Customer, so it lives on this same row rather than a
    # second table (avoids a redundant join). All NULL until a subscription is first created.
    stripe_subscription_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # The price the CURRENT subscription is on — compared against a request's price to decide
    # create vs. update vs. no-op.
    stripe_price_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Written by the Stripe webhook handler (add-billing-stripe-webhooks) on
    # customer.subscription.updated/.deleted (Stripe's own status, verbatim) and
    # invoice.payment_failed ("past_due"). NULL until the first such event arrives.
    subscription_status: Mapped[str | None] = mapped_column(String, nullable=True)
    # Incremented on every `subscriptions.create` attempt (never on `modify`) and folded into
    # that call's idempotency key, so each attempt gets a distinct key rather than reusing one
    # across retries.
    subscription_create_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )


class WebhookEvent(Base):
    """Idempotency record for a processed Stripe webhook event (add-billing-stripe-webhooks).
    `UNIQUE(stripe_event_id)` is the idempotency guarantee — a redelivered event's insert
    violates the constraint and is treated as an already-processed no-op, mirroring
    `Usage.request_id`'s discipline. A row's mere existence IS the record; there is no
    `processed`/status column, since this phase applies the effect synchronously in the same
    request/transaction, not as a queued job."""

    __tablename__ = "webhook_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    stripe_event_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
