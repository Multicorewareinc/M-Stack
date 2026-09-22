"""App entrypoint. Builds the FastAPI app, wires settings + DB engine/sessionmaker + seed +
routers. First DB-backed service in the repo (ADR-015).

Run: uvicorn main:create_app --factory --host 0.0.0.0 --port 8000
(Production DDL is applied by `alembic upgrade head` in docker-entrypoint.sh, not here.)
"""

from __future__ import annotations

import json
import logging
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from db import Base, build_engine, build_sessionmaker
from fastapi import FastAPI
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from seed import seed_permissions, seed_plans
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.responses import Response

from errors import install_error_handlers
from modules.auth.router import router as auth_router
from modules.organizations.router import internal_router as organizations_internal_router
from modules.organizations.router import router as organizations_router
from modules.permissions.router import internal_router as permissions_internal_router
from modules.permissions.router import router as permissions_router
from modules.plans.router import router as plans_router
from modules.platform_summary.router import router as platform_summary_router
from modules.user_directory.router import platform_router as user_directory_platform_router
from modules.user_directory.router import router as user_directory_router
from modules.user_provisioning.router import router as user_provisioning_router
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
    org_client: httpx.AsyncClient | None = None,
    billing_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    settings = settings or Settings()
    _setup_logging(settings.log_level)

    # Owns-it idiom (mirror rate-limiter-tpm's owns_redis): build + own the engine only when
    # one is not injected. Offline tests inject an aiosqlite engine and own its lifecycle.
    owns_engine = engine is None
    if engine is None:
        engine = build_engine(settings.database_url)
    sessionmaker = build_sessionmaker(engine)

    # Owns-it outbound client to Org CP's internal API (org-creation workflow, ADR-018/AD-03).
    # Tests inject an httpx.AsyncClient(transport=MockTransport(...)); aclose only if we built it.
    owns_org_client = org_client is None
    if org_client is None:
        org_client = httpx.AsyncClient(
            base_url=settings.org_cp_internal_url,
            headers={"Authorization": f"Bearer {settings.service_api_key}"},
            timeout=settings.org_timeout_ms / 1000,
        )

    # Owns-it outbound client to billing's internal API (best-effort subscription notification,
    # ADR-032/add-billing-plan-subscription-linkage) — mirrors org_client exactly.
    owns_billing_client = billing_client is None
    if billing_client is None:
        billing_client = httpx.AsyncClient(
            base_url=settings.billing_internal_url,
            headers={"Authorization": f"Bearer {settings.service_api_key}"},
            timeout=settings.billing_timeout_ms / 1000,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Schema bootstrap split (AD-04): create_all runs ONLY on an injected engine (the
        # aiosqlite/test path). The production-built engine gets its schema from Alembic.
        if not owns_engine:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        async with sessionmaker() as session:
            await seed_plans(session, settings.seed_plans)
            await seed_permissions(session, settings.seed_permissions)
        yield
        if owns_engine:
            await engine.dispose()
        if owns_org_client:
            await org_client.aclose()
        if owns_billing_client:
            await billing_client.aclose()

    app = FastAPI(title="Admin Control Plane", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = sessionmaker
    app.state.org_client = org_client
    app.state.billing_client = billing_client

    install_error_handlers(app)
    # Public super-admin auth surface (D8) — /api/auth, NOT behind require_admin_key.
    app.include_router(auth_router)
    app.include_router(plans_router)
    app.include_router(organizations_router)
    app.include_router(organizations_internal_router)
    app.include_router(user_provisioning_router)
    app.include_router(permissions_router)
    app.include_router(permissions_internal_router)
    app.include_router(user_directory_router)
    app.include_router(user_directory_platform_router)
    app.include_router(platform_summary_router)

    @app.get("/health", tags=["ops"])
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/metrics", tags=["ops"])
    async def metrics() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app
