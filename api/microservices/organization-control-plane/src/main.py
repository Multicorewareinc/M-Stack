"""App entrypoint. Builds the FastAPI app, wires settings + DB engine/sessionmaker + routers.
Mirrors admin-control-plane's create_app (ADR-020). The only seeding is the optional, e2e/CI-only
fixed verify API key (SEED_API_KEY, src/seed.py) — a no-op in production where it is unset.

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
from seed import seed_api_key
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.responses import Response

from errors import install_error_handlers
from modules.api_keys.router import internal_router as api_keys_internal_router
from modules.api_keys.router import router as api_keys_router
from modules.audit.router import internal_router as audit_internal_router
from modules.audit.router import router as audit_router
from modules.auth.router import router as auth_router
from modules.invitations.router import internal_router as invitations_internal_router
from modules.invitations.router import platform_router as invitations_platform_router
from modules.invitations.router import router as invitations_router
from modules.organizations.router import internal_router as organizations_internal_router
from modules.organizations.router import router as organizations_router
from modules.permissions.router import router as permissions_router
from modules.rbac.router import internal_router as rbac_internal_router
from modules.rbac.router import router as rbac_router
from modules.roles.router import internal_router as roles_internal_router
from modules.roles.router import router as roles_router
from modules.usage.router import router as usage_router
from modules.users.router import api_router as users_api_router
from modules.users.router import internal_router as users_internal_router
from modules.users.router import platform_router as users_platform_router
from modules.users.router import router as users_router
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
    admin_client: httpx.AsyncClient | None = None,
    billing_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    settings = settings or Settings()
    _setup_logging(settings.log_level)

    # Owns-it idiom (mirror admin-control-plane): build + own the engine only when one is not
    # injected. Offline tests inject an aiosqlite engine and own its lifecycle.
    owns_engine = engine is None
    if engine is None:
        engine = build_engine(settings.database_url_org)
    sessionmaker = build_sessionmaker(engine)

    # Owns-it outbound client to Admin CP's internal API (validate permission ids, ADR-018/019).
    # Tests inject an httpx.AsyncClient(transport=MockTransport(...)); we aclose only if we built it.
    owns_admin_client = admin_client is None
    if admin_client is None:
        admin_client = httpx.AsyncClient(
            base_url=settings.admin_cp_internal_url,
            headers={"Authorization": f"Bearer {settings.service_api_key}"},
            timeout=settings.admin_timeout_ms / 1000,
        )

    # Owns-it outbound client to billing's internal API (usage proxy, add-org-cp-usage-proxy) —
    # second instance of the admin_client pattern above. Tests inject an
    # httpx.AsyncClient(transport=MockTransport(...)); we aclose only if we built it.
    owns_billing_client = billing_client is None
    if billing_client is None:
        billing_client = httpx.AsyncClient(
            base_url=settings.billing_internal_url,
            headers={"Authorization": f"Bearer {settings.service_api_key}"},
            timeout=settings.billing_timeout_ms / 1000,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Schema bootstrap split (ADR-015a): create_all runs ONLY on an injected engine (the
        # aiosqlite/test path). The production-built engine gets its schema from Alembic.
        if not owns_engine:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        # e2e/CI only: seed the fixed verify key after the schema exists (create_all above on the test
        # engine; Alembic before uvicorn boots in prod). A no-op when settings.seed_api_key is unset.
        if settings.seed_api_key:
            async with sessionmaker() as session:
                await seed_api_key(session, settings.seed_api_key)
        yield
        if owns_engine:
            await engine.dispose()
        if owns_admin_client:
            await admin_client.aclose()
        if owns_billing_client:
            await billing_client.aclose()

    app = FastAPI(title="Organization Control Plane", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = sessionmaker
    app.state.admin_client = admin_client
    app.state.billing_client = billing_client

    install_error_handlers(app)
    # users_internal_router's literal /user-counts and /active-user-count routes must be
    # registered before organizations_internal_router's catch-all GET /{organization_id} — both
    # sit under the same /internal/v1/organizations prefix, and Starlette matches path params
    # against any string at the routing layer (a UUID.UUID mismatch only 422s AFTER matching),
    # so the catch-all would otherwise shadow them.
    app.include_router(users_internal_router)
    app.include_router(users_platform_router)
    app.include_router(organizations_internal_router)
    app.include_router(organizations_router)
    app.include_router(permissions_router)
    app.include_router(usage_router)
    app.include_router(users_router)
    app.include_router(users_api_router)
    app.include_router(roles_router)
    app.include_router(roles_internal_router)
    app.include_router(rbac_router)
    app.include_router(rbac_internal_router)
    # Public end-user auth (/api/auth) — NOT behind require_service_key / require_org_context (D6).
    app.include_router(auth_router)
    # Public end-user API-key lifecycle (/api/api-keys) — same JWT surface (ADR-027).
    app.include_router(api_keys_router)
    # Audit-log reads: org-scoped JWT surface (/api/audit-logs) + cross-org service surface for Admin CP.
    app.include_router(audit_router)
    app.include_router(audit_internal_router)
    # Invitations: org-admin JWT surface + public accept (/api/invitations) and Admin CP's internal issue route.
    app.include_router(invitations_router)
    app.include_router(invitations_internal_router)
    app.include_router(invitations_platform_router)
    # Internal key verify surface for the model-gateway (/internal/v1/api-keys) — service bearer (SP-02).
    app.include_router(api_keys_internal_router)

    @app.get("/health", tags=["ops"])
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/metrics", tags=["ops"])
    async def metrics() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app
