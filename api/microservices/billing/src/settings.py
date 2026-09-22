"""Service configuration. Single Settings class, read from env (and optional .env)."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    log_level: str = "INFO"

    # Billing's own database (SQLAlchemy async URL, asyncpg driver). Alembic reads this too.
    # Owned by this service alone (database-per-service, ADR-003/ADR-006/ADR-015) — no other
    # service reads or writes it.
    database_url: str = "postgresql+asyncpg://billing:billing@localhost:5432/billing_service"

    # Async metering via the ENRICHED event backbone (ADR-030/ADR-031). Empty = no consumer =>
    # nothing is metered. When set, the subject/stream MUST be the enriched pair (ADR-030
    # AD-07) — enforced by fail-fast startup validation in main.py, not by defaults alone.
    event_backbone_url: str = ""
    event_stream_name: str = "GATEWAY_EVENTS_ENRICHED"
    event_stream_subject: str = "gateway.events.enriched"
    event_durable_name: str = "billing-metering"

    # Stripe reporter (ADR-031 AD-05/AD-06) — a second surface of this same service that
    # drains `outbox` rows to Stripe. Empty STRIPE_SECRET_KEY => inert: zero Stripe calls,
    # zero outbox mutations, rows stay pending. Enabling reporting is a pure config change.
    stripe_secret_key: str = ""
    # The Meter Event payload key `principal` is sent under. Documented limitation (AD-06):
    # `principal` is an internal org_id, not yet a real Stripe Customer ID, until the
    # org->customer mapping lands.
    stripe_meter_event_name: str = "tokens_used"
    stripe_meter_customer_field: str = "stripe_customer_id"
    reporter_poll_interval_seconds: int = 5
    reporter_batch_size: int = 50
    reporter_max_attempts: int = 5
    reporter_backoff_base_seconds: int = 30
    reporter_backoff_max_seconds: int = 3600

    # Gates /internal/v1/* (add-billing-plan-subscription-linkage) — admin-control-plane's
    # outbound bearer to this endpoint. No default: an unset key fails closed as a 500 the first
    # time require_service_key runs, never a silent bypass (mirrors admin-control-plane's
    # service_api_key convention exactly).
    service_api_key: str | None = None

    # Gates POST /webhooks/stripe (add-billing-stripe-webhooks). Empty = INERT: the endpoint
    # returns 501 without reading the request body, verifying a signature, or touching the
    # database — never accepts an unverified webhook (ADR-006 rule 3, mirrors stripe_secret_key).
    stripe_webhook_secret: str = ""

    # Dev-only: auto-starts `stripe listen --forward-to <stripe_cli_forward_url>` in the app
    # lifespan (see stripe_cli.py) so a developer doesn't have to run it by hand. False by
    # default — never enable in a deployed environment; a deployment registers a real webhook
    # endpoint with Stripe instead.
    enable_stripe_cli: bool = False
    stripe_cli_forward_url: str = "http://localhost:8000/webhooks/stripe"
