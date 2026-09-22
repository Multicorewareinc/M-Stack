"""App entrypoint. Builds the FastAPI app, wires settings + consumer + routes.

Run: uvicorn main:create_app --factory --host 0.0.0.0 --port 8000

The enricher is stateless (no DB, no Redis). Its work is the JetStream consumer strain
(ADR-030): consume gateway.events `response` events, guarantee a token count, republish to
gateway.events.enriched. Empty EVENT_BACKBONE_URL => no consumer => the service is inert.
"""

from __future__ import annotations

import json
import logging
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from consumer import EnricherConsumer
from fastapi import FastAPI
from router import router

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


def create_app(settings: Settings | None = None, *, consumer=None, http_client=None) -> FastAPI:
    settings = settings or Settings()
    _setup_logging(settings.log_level)

    # Build the backbone consumer only when configured and not injected (tests inject their
    # own / none). Empty EVENT_BACKBONE_URL => no consumer => the enricher is inert.
    owns_http_client = http_client is None
    own_consumer = consumer is None and bool(settings.event_backbone_url)
    if own_consumer:
        if http_client is None:
            import httpx

            http_client = httpx.AsyncClient()
        consumer = EnricherConsumer(settings, http_client=http_client)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if own_consumer:
            await consumer.start()  # best-effort; inactive (not fatal) if the backbone is down
        yield
        if own_consumer:
            await consumer.stop()
            if owns_http_client:
                await http_client.aclose()

    app = FastAPI(title="Event Enricher", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.consumer = consumer

    app.include_router(router)
    return app
