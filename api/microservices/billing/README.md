# billing

Meters LLM token usage per `principal` from the enriched event stream (ADR-030) into a
durable, idempotent Postgres ledger — the foundation for Stripe-based billing (ADR-031).
Database-backed, so this service deviates from the flat ADR-003 boilerplate only by adding
`db.py`/`models.py` + Alembic (ADR-015 pattern) — its domain is a single cohesive concern
(a usage ledger and its outbox), so it does NOT use the `src/modules/` package layout
`admin-control-plane` uses for genuinely multi-module domain state.

A **Stripe reporter** runs as a second surface of this same service (not a separate
deployable, ADR-031 AD-05) that drains the `outbox` table and pushes usage to Stripe.

## What it does

Durably consumes `response` events from `gateway.events.enriched` (produced by the
`enricher` service). For each event:

1. **Countable** (`usage.total_tokens` a present number) — in one Postgres transaction:
   insert a `usage` row and a `pending` `outbox` row. Both committed together, or neither.
2. **Un-countable** (`usage.source == "none"`, or a malformed/non-numeric count) — insert a
   `usage` row only (`total_tokens = NULL`), for audit. No outbox row: never billed on a
   guess.

Idempotency is a **Postgres `UNIQUE(request_id)` constraint**, not an app-level dedupe
store — a redelivered event's insert violates the constraint and is treated as an
already-processed no-op (still acked). Any *other* integrity failure, or any failure during
commit, is a genuine fault: the message is **not** acked, so JetStream redelivers once the
fault clears. A sustained Postgres outage therefore pauses metering rather than silently
dropping revenue — messages are safe on the retained enriched stream.

## Schema

- `usage`: one row per processed event. `request_id` (unique), `principal`, `model`,
  `total_tokens` (nullable), `source`, `reason` (nullable), `prompt_tokens`/
  `completion_tokens` (nullable, when the provider supplied a breakdown), `ts` (the event's
  own timestamp), `created_at`.
- `outbox`: one `pending` row per countable event. `request_id`, `payload` (JSON — the
  fields the Stripe reporter needs: `request_id, principal, model, total_tokens, source, ts`
  — self-sufficient, no join-back to `usage`), `status` (`pending`→`sent`/`failed`),
  `attempts`, `next_attempt_at`, `created_at`, `sent_at`.
- `stripe_customers`: maps an org (`principal`, UNIQUE) to a real Stripe Customer
  (`stripe_customer_id`), `created_at`, `updated_at`. Populated lazily by the Stripe reporter
  (see below), never by the metering consumer. Also carries subscription linkage
  (`stripe_subscription_id`, `stripe_price_id` — the price the current subscription is on —
  and `subscription_status`, written by the Stripe webhook endpoint below) — extended here
  rather than a second table, since a Subscription is 1:1 with a Customer.
- `webhook_events`: idempotency record for processed Stripe webhook events
  (`stripe_event_id` UNIQUE, `event_type`, `payload` JSON, `created_at`). A row's existence IS
  the record — a redelivered event is a no-op, mirroring `usage.request_id`'s discipline.

## Stripe reporter

A background poll loop (interval-based — there's no event to react to; `outbox` rows are
written by a separate process, the metering consumer above) drains `pending` `outbox` rows
and pushes each to Stripe **individually** (per-event, not aggregated), with a Stripe
idempotency key derived from `request_id` so neither the reporter's own retry nor a
network-level retry can double-report.

**Outbox lifecycle:** `pending` → `sent` (success, terminal) | `pending` (transient failure,
backed off, `attempts` incremented) | `failed` (a transient failure that reached
`REPORTER_MAX_ATTEMPTS`, OR any permanent failure — e.g. an invalid request — on its first
attempt; terminal either way, but the row is **never deleted**, remaining durable for ops
follow-up). **Resetting a `failed` row back to `pending` for manual retry must also reset
`attempts` to 0**, or it will re-fail to `failed` after a single further attempt.

**Inert by default:** the reporter object is always constructed, but its `run_once()` is a
pure no-op — zero Stripe calls, zero outbox mutations — whenever `STRIPE_SECRET_KEY` is
unset. Enabling reporting is a pure config change.

**Org→Stripe-customer mapping (AD-06, resolved):** before reporting a row, the reporter
resolves `principal` (org id) to a real Stripe Customer via `create_or_get_stripe_customer` —
a lookup against `stripe_customers`, creating a Stripe Customer (`metadata={"org_id":
principal}`) on the first miss and persisting the mapping, then reusing it on every
subsequent event for that org. The Meter Event's `STRIPE_METER_CUSTOMER_FIELD` now carries
the **resolved** `stripe_customer_id`, not the raw `principal`. Customer creation uses its own
stable, per-org idempotency key (`billing:customer:{principal}`, distinct from the per-event
`billing:{request_id}` key) so a reporter restart mid-cycle cannot create a duplicate Stripe
Customer. A customer-resolution failure is classified with the identical
transient/permanent discipline as usage reporting and applies the row's existing
attempts/backoff/terminal-failure bookkeeping — `report_to_stripe` is never called against a
nonexistent customer. A `"sent"` outcome now proves correct billing attribution, not merely
delivery to Stripe's ingestion API.

**Single-instance assumption:** the poll query has no row-claiming step. Stripe-side
double-reporting is still prevented by the idempotency key (derived purely from
`request_id`, independent of instance count), but running more than one `billing` replica
could race on a row's local `status`/`attempts` bookkeeping. Upgrade path if this service
ever scales horizontally: `SELECT ... FOR UPDATE SKIP LOCKED` or a `pending`→`in_flight`
claim step.

## Internal subscription-linkage endpoint

`POST /internal/v1/subscriptions {org_id, stripe_price_id}` (behind `require_service_key` —
this service's first bearer-gated HTTP write surface) is called by admin-control-plane
best-effort on org creation and plan change (ADR-032). It resolves/creates the org's Stripe
Customer (reusing `create_or_get_stripe_customer` — a brand-new org may have no usage event
yet), then creates or updates a Stripe Subscription against the given price. Two independent
inert gates: a `null`/empty `stripe_price_id` is a no-op checked before anything else runs
(`outcome: "inert"`); `STRIPE_SECRET_KEY` unset entirely is a second, config-level inert gate
(also `outcome: "inert"`, and — unlike the price gate — opens no database session at all).
Every classified Stripe outcome (`created`/`updated`/`noop`/`transient_failure`/
`permanent_failure`) is a `200` response, never a `4xx`/`5xx` — a Stripe-side failure is
billing's own operational concern, not the caller's, since the caller's own reaction is
already best-effort regardless (ADR-032). Subscription create/update uses two DIFFERENT
idempotency-key shapes: `billing:subscription:create:{principal}` (stable per org) vs.
`billing:subscription:update:{principal}:{stripe_price_id}` (scoped to the target price, so a
later, different price change is never silently deduped against an earlier one).

## Stripe webhooks

`POST /webhooks/stripe` verifies the `Stripe-Signature` header against `STRIPE_WEBHOOK_SECRET`
via the Stripe SDK's own `stripe.Webhook.construct_event` — never a hand-rolled check. An
invalid/missing signature is `400`. **Inert by default**: an unset `STRIPE_WEBHOOK_SECRET`
returns `501` immediately — no body read, no signature check, no DB session (the strongest of
this service's inert gates, since there is nothing safe to do with an unverifiable payload).

Handles `customer.subscription.updated`, `customer.subscription.deleted`, and
`invoice.payment_failed`, updating the matching `stripe_customers` row's `subscription_status`
— looked up by `stripe_subscription_id` (Stripe's own id), never by `principal`, since webhook
payloads don't carry billing's internal org id. `customer.subscription.updated`/`.deleted` copy
Stripe's own subscription `status` value verbatim; `invoice.payment_failed` sets `"past_due"`.
An event for a subscription this instance doesn't track is a logged no-op (still acknowledged
`200`), not an error.

Idempotent under Stripe's at-least-once delivery via `webhook_events.stripe_event_id UNIQUE` —
a redelivered event is a no-op. Processing is ONE transaction with a single trailing commit
(not a commit-per-step): the idempotency insert and any `subscription_status` update land
together, atomically, so a mid-request crash can never durably mark an event "seen" while
losing its effect.

**Open product question, not implemented this phase**: whether a `canceled`/`past_due`
`subscription_status` should automatically pause metering (`BillingConsumer`) or Stripe
reporting (`StripeReporter`). Today, neither is affected — usage keeps being metered and
reported regardless of `subscription_status`.

## Config

See `.env.example`. Notably: `EVENT_STREAM_SUBJECT`/`EVENT_STREAM_NAME` must exactly match
the enriched pair when `EVENT_BACKBONE_URL` is set (ADR-030 AD-07) — a mismatch (raw
subject, a typo, or an inconsistent pairing) fails startup with a `RuntimeError` **before**
any database engine or consumer is constructed. This is a new, distinct failure mode: the
container does not start at all, not a degraded running state.

## Ops

- `GET /health` — liveness. `GET /metrics` — Prometheus:
  `billing_usage_persisted_total{countable="true"|"false"}`, `billing_consumer_active` gauge,
  `billing_stripe_reported_total{outcome="sent"|"transient_failure"|"terminal_failure"}`,
  `billing_stripe_customer_resolved_total{outcome="created"|"reused"|"failed"}` (a transient,
  non-ceiling customer-resolution failure increments none of these three — see the
  `stripe_customers`/reporter sections above), `billing_reporter_active` gauge (both gauges
  are the readiness signal; no `/ready`, matching the `enricher` service's precedent).
- No public (unauthenticated) write API — the JetStream consumer, the Stripe reporter, and the
  `require_service_key`-gated `/internal/v1/subscriptions` endpoint above are the only write
  paths.

## Ops notes

- `GATEWAY_EVENTS_ENRICHED`'s retention must cover the metering consumer's worst-case
  Postgres-outage window (the accepted trade-off of never silently dropping a metered
  event). Postgres integration testing requires `alembic upgrade head` applied first.
- Resetting a `failed` outbox row to `pending` for manual retry must also reset `attempts`
  to 0 (see Stripe reporter section above).

## Tests

`api/tests/billing/tests/` — offline (injected in-memory aiosqlite engine, no live NATS/
Postgres) and `api/tests/billing/integration/` — a real-Postgres run (`BILLING_PG_DSN`
gated) confirming the Alembic-migrated schema and its `UNIQUE` constraints. See ADR-031.
