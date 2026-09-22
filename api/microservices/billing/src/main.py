"""App entrypoint. Builds the FastAPI app, wires settings + DB engine/sessionmaker + consumer +
routes.

Run: uvicorn main:create_app --factory --host 0.0.0.0 --port 8000
(Production DDL is applied by `alembic upgrade head` in docker-entrypoint.sh, not here.)
"""

from __future__ import annotations

import json
import logging
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from consumer import BillingConsumer
from db import Base, build_engine, build_sessionmaker
from fastapi import FastAPI
from reporter import StripeReporter
from router import internal_router, router
from sqlalchemy.ext.asyncio import AsyncEngine
from stripe_cli import StripeCLIManager

from errors import install_error_handlers
from settings import Settings


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _setup_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())


def create_app(
    settings: Settings | None = None,
    *,
    engine: AsyncEngine | None = None,
    consumer=None,
    reporter=None,
) -> FastAPI:
    settings = settings or Settings()
    _setup_logging(settings.log_level)

    # Fail fast at startup, not on every message, if this service is wired to consume anything
    # other than the enriched stream while a backbone is configured (ADR-030 AD-07). Allowlist,
    # not blocklist: BOTH subject and stream name must equal the enriched pair. Runs BEFORE any
    # engine/consumer construction, mirroring rate-limiter-tpm's validation exactly.
    if settings.event_backbone_url:
        if settings.event_stream_subject != "gateway.events.enriched":
            raise RuntimeError(
                f"EVENT_STREAM_SUBJECT={settings.event_stream_subject!r} is not the "
                "enriched subject 'gateway.events.enriched' (ADR-030 AD-07: enabling "
                "billing requires consuming the enricher's output, not the raw gateway "
                "stream)"
            )
        if settings.event_stream_name != "GATEWAY_EVENTS_ENRICHED":
            raise RuntimeError(
                f"EVENT_STREAM_NAME={settings.event_stream_name!r} is not the enriched "
                "stream 'GATEWAY_EVENTS_ENRICHED' (ADR-030 AD-07: subject and stream "
                "name must name the same enriched pair)"
            )

    # Owns-it idiom (mirror admin-control-plane): build + own the engine only when one is not
    # injected. Offline tests inject an aiosqlite engine and own its lifecycle.
    owns_engine = engine is None
    if engine is None:
        engine = build_engine(settings.database_url)
    sessionmaker = build_sessionmaker(engine)

    own_consumer = consumer is None and bool(settings.event_backbone_url)
    if own_consumer:
        consumer = BillingConsumer(settings, sessionmaker)

    # The reporter is ALWAYS constructed (own_reporter = reporter is None, unconditional — no
    # second clause on stripe_secret_key, unlike own_consumer's two-part gate above). Its
    # inert-by-default behavior is internal (StripeReporter.run_once is a no-op when no Stripe
    # client is configured, ADR-031 AD-06) rather than the object simply not existing — this is
    # what lets `app.state.reporter.run_once()` be called directly to prove the real inert gate.
    own_reporter = reporter is None
    if own_reporter:
        reporter = StripeReporter(settings, sessionmaker)

    stripe_cli_manager = (
        StripeCLIManager(settings.stripe_cli_forward_url, settings.stripe_secret_key)
        if settings.enable_stripe_cli
        else None
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # Schema bootstrap split (mirrors admin-control-plane AD-04): create_all runs ONLY on
        # an injected engine (the aiosqlite/test path). The production-built engine gets its
        # schema from Alembic (docker-entrypoint.sh).
        if not owns_engine:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        if own_consumer:
            await consumer.start()  # best-effort; inactive (not fatal) if the backbone is down
        if own_reporter:
            await reporter.start()
        if stripe_cli_manager is not None:
            # Best-effort, like the consumer: a missing `stripe` binary logs and moves on
            # rather than blocking startup (ENABLE_STRIPE_CLI is dev-only tooling, never a
            # production dependency).
            stripe_cli_manager.start()
        yield
        if stripe_cli_manager is not None:
            stripe_cli_manager.stop()
        if own_reporter:
            await reporter.stop()  # completes before engine.dispose(), same as consumer.stop()
        if own_consumer:
            await consumer.stop()
        if owns_engine:
            await engine.dispose()

    app = FastAPI(title="Billing", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = sessionmaker
    app.state.consumer = consumer
    app.state.reporter = reporter
    app.state.stripe_cli_manager = stripe_cli_manager
    # Reused by the internal subscription-linkage endpoint (add-billing-plan-subscription-
    # linkage) so it never constructs a second Stripe client — via the property, not the
    # private attribute.
    app.state.stripe_client = reporter.stripe_client

    install_error_handlers(app)
    app.include_router(router)
    app.include_router(internal_router)
    return app
