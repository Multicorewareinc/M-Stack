"""App entrypoint. Builds the FastAPI app, wires settings + service + routes.

Run: uvicorn main:create_app --factory --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import json
import logging
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from consumer import RpmConsumer
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


def create_app(settings: Settings | None = None, *, redis_client=None, consumer=None) -> FastAPI:
    settings = settings or Settings()
    _setup_logging(settings.log_level)

    owns_redis = redis_client is None
    if redis_client is None:
        # Lazy import so offline tests (which inject a fake client) never need the
        # redis package. Real client built only when not injected.
        from redis.asyncio import from_url

        redis_client = from_url(settings.valkey_url, encoding="utf-8", decode_responses=True)

    plan_client = PlanClient(settings.admin_cp_url, settings.service_api_key, ttl=settings.plan_cache_ttl)
    service = RateLimiterService(settings, redis_client=redis_client, plan_client=plan_client)

    # Async counting strain (ADR-013). Build the backbone consumer only when configured and
    # not injected (tests inject their own / none). Empty EVENT_BACKBONE_URL => no consumer
    # => counters never advance => /check fails open.
    own_consumer = consumer is None and bool(settings.event_backbone_url)
    if own_consumer:
        consumer = RpmConsumer(settings, service)

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

    app = FastAPI(title="Rate Limiter (RPM)", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.service = service
    app.state.consumer = consumer

    install_error_handlers(app)
    app.include_router(router)
    return app
