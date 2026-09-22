"""App entrypoint. Builds the FastAPI app, wires settings + service + routes.

Run: uvicorn main:create_app --factory --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import json
import logging
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import redis.asyncio as aioredis
from events import EventPublisher
from fastapi import FastAPI
from middleware import RateLimitHeaderMiddleware
from router import router
from service import GatewayService

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
    client: httpx.AsyncClient | None = None,
    policy_client: httpx.AsyncClient | None = None,
    event_publisher: EventPublisher | None = None,
    redis: aioredis.Redis | None = None,
    org_cp_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    settings = settings or Settings()
    _setup_logging(settings.log_level)

    service = GatewayService(settings, client=client)

    # API-key verify path (SP-02, ADR-026). Build (unless injected) the Redis read-through cache
    # client and the Org CP internal client; own — and so close — only what we build. Tests inject
    # an in-memory Redis stub and a MockTransport Org CP client.
    own_redis = redis is None
    if redis is None:
        redis = aioredis.Redis.from_url(settings.valkey_url)
    own_org_cp_client = org_cp_client is None
    if org_cp_client is None:
        org_cp_client = httpx.AsyncClient(
            base_url=settings.org_cp_internal_url,
            headers={"Authorization": f"Bearer {settings.org_verify_api_key}"},
            timeout=settings.request_timeout,
        )

    # Policy chain (docs/policy-plane.md). No endpoints => no client built (inert).
    endpoints = settings.policy_endpoint_list()
    if policy_client is None and endpoints:
        policy_client = httpx.AsyncClient(timeout=settings.policy_timeout_ms / 1000)

    # Event backbone (ADR-007/008/009). Empty url => no publisher built (inert).
    # An injected publisher (tests) is treated as already-ready: the app owns — and so
    # connects/closes — only a publisher it built itself.
    own_publisher = event_publisher is None and bool(settings.event_backbone_url)
    if own_publisher:
        event_publisher = EventPublisher(
            settings.event_backbone_url,
            stream_name=settings.event_stream_name,
            subject=settings.event_stream_subject,
            capture_bodies=settings.event_capture_bodies,
            max_inflight=settings.event_max_inflight,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if own_publisher:
            await event_publisher.connect()  # best-effort; degrades, never crashes boot
        yield
        await service.aclose()
        if policy_client is not None:
            await policy_client.aclose()
        if own_publisher:
            await event_publisher.aclose()
        if own_redis:
            await redis.aclose()
        if own_org_cp_client:
            await org_cp_client.aclose()

    app = FastAPI(title="Model Gateway", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.service = service
    app.state.policy_endpoints = endpoints
    app.state.policy_client = policy_client
    app.state.policy_fail_open = settings.policy_fail_mode.lower() == "open"
    app.state.event_publisher = event_publisher
    app.state.redis = redis
    app.state.org_cp_client = org_cp_client

    # Generic X-RateLimit-* header stamping (ADR-012). Always mounted; inert unless the
    # policy chain stashes numbers on request.state.ratelimit. Pure ASGI (SSE-safe).
    app.add_middleware(RateLimitHeaderMiddleware)

    install_error_handlers(app)
    app.include_router(router)
    return app
