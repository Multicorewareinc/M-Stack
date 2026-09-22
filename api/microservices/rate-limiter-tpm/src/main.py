"""App entrypoint. Builds the FastAPI app, wires settings + service + routes.

Run: uvicorn main:create_app --factory --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import json
import logging
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from consumer import TpmConsumer
from fastapi import FastAPI
from plan_client import PlanClient
from router import router
from service import RateLimiterService

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
    settings: Settings | None = None, *, redis_client=None, consumer=None
) -> FastAPI:
    settings = settings or Settings()
    _setup_logging(settings.log_level)

    # Fail fast at startup, not on every request, if this service is wired to consume
    # anything other than the enriched stream while a backbone is configured (ADR-030
    # AD-07). Allowlist, not blocklist: BOTH the subject and stream name must equal the
    # enriched pair — a raw subject, a typo'd value, or a mismatched pairing of the two
    # would otherwise bind silently against a stream with no guaranteed usage (or no
    # producer at all), discoverable only later via /ready. No consumer runs at all when
    # EVENT_BACKBONE_URL is empty, so nothing to validate in that case.
    if settings.event_backbone_url:
        if settings.event_stream_subject != "gateway.events.enriched":
            raise RuntimeError(
                f"EVENT_STREAM_SUBJECT={settings.event_stream_subject!r} is not the "
                "enriched subject 'gateway.events.enriched' (ADR-030 AD-07: enabling "
                "TPM requires consuming the enricher's output, not the raw gateway "
                "stream)"
            )
        if settings.event_stream_name != "GATEWAY_EVENTS_ENRICHED":
            raise RuntimeError(
                f"EVENT_STREAM_NAME={settings.event_stream_name!r} is not the enriched "
                "stream 'GATEWAY_EVENTS_ENRICHED' (ADR-030 AD-07: subject and stream "
                "name must name the same enriched pair)"
            )

    owns_redis = redis_client is None
    if redis_client is None:
        # Lazy import so offline tests (which inject a fake client) never need the
        # redis package. Real client built only when not injected.
        from redis.asyncio import from_url

        redis_client = from_url(settings.valkey_url, encoding="utf-8", decode_responses=True)

    plan_client = PlanClient(settings.admin_cp_url, settings.service_api_key, ttl=settings.plan_cache_ttl)
    service = RateLimiterService(settings, redis_client=redis_client, plan_client=plan_client)

    # Async counting strain (AD-01/AD-02, ADR-030). Build the backbone consumer only when
    # configured and not injected (tests inject their own / none). Empty
    # EVENT_BACKBONE_URL => no consumer => counters never advance => /check fails open.
    # No HTTP client here: the consumer no longer calls the tokenizer itself (ADR-030
    # AD-02 — that fallback moved to the `enricher` service).
    own_consumer = consumer is None and bool(settings.event_backbone_url)
    if own_consumer:
        consumer = TpmConsumer(settings, service)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if own_consumer:
            await consumer.start()  # best-effort; inactive (not fatal) if the backbone is down
        yield
        if own_consumer:
            await consumer.stop()
        await plan_client.aclose()
        if owns_redis:
            await redis_client.aclose()

    app = FastAPI(title="Rate Limiter (TPM)", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.service = service
    app.state.consumer = consumer

    install_error_handlers(app)
    app.include_router(router)
    return app
