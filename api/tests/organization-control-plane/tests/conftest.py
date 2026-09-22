"""Test helpers for Org CP — build the app with an injected in-memory aiosqlite engine, so
tests never touch Postgres (mirrors admin-control-plane's conftest, ADR-020).

Two gotchas (same as admin-CP):
- `StaticPool` keeps the single in-memory connection alive so the schema persists across the
  app's connections within a test.
- The `TestClient` MUST be entered as a context manager so lifespan startup runs create_all on
  the injected engine.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from main import create_app
from settings import Settings

SERVICE_KEY = "test-service-key"
# Distinct from SERVICE_KEY on purpose — the security fix under test is that the broad
# BFF/portal-backend bearer and the model-gateway's narrow verify-only bearer are two separate
# secrets (see dependencies.py's require_service_key / require_mg_service_key). A test that
# accidentally reused SERVICE_KEY here would silently defeat that check.
MG_KEY = "test-mg-verify-key"

# The default master permission catalog a real Admin CP always has seeded (seed.py). The Org CP
# org-init seeds default roles bound to these slugs (ADR-029/AD-04), so the stub Admin CP must
# present them or every org-init would fail closed. Fixed ids keep assertions stable.
DEFAULT_PERMISSION_SLUGS = (
    "users.read", "users.create", "users.update", "users.delete",
    "roles.read", "roles.create", "roles.update", "roles.delete",
    "organizations.read", "organizations.update", "apikey.manage",
)


def default_permission_catalog():
    """A stub master catalog (one {id, slug} per default slug) with deterministic ids."""
    return [
        {"id": str(uuid.uuid5(uuid.NAMESPACE_DNS, slug)), "slug": slug}
        for slug in DEFAULT_PERMISSION_SLUGS
    ]


def make_engine():
    return create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)


def make_admin_client(
    known_permission_ids=(), *, unavailable=False, permission_catalog=None, plan=None
):
    """A stub Admin CP internal client via MockTransport, routed by exact path:
    - GET /internal/v1/permissions/{id}: 200 for a known id, 404 otherwise (role-permission
      composition's per-id validation, ADR-019).
    - GET /internal/v1/permissions: 200 with `permission_catalog` (defaults to []) — the
      permissions-catalog proxy (SP-13/org-permission-catalog wiring).
    - GET /internal/v1/organizations/{id}/plan: 200 with `plan` (defaults to a stub Free-like
      plan) — the organization-summary proxy's plan resolution.
    `unavailable=True` raises a transport error to exercise the fail-closed (502) path for all
    three."""
    known = {str(p) for p in known_permission_ids}
    catalog = permission_catalog if permission_catalog is not None else default_permission_catalog()
    plan_body = plan if plan is not None else {
        "id": str(uuid.uuid4()),
        "name": "Free",
        "tpm": 10000,
        "rpm": 60,
        "quota_monthly_tokens": 1000000,
        "is_default": True,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if unavailable:
            raise httpx.ConnectError("admin cp down", request=request)
        path = request.url.path
        if path == "/internal/v1/permissions":
            return httpx.Response(200, json=catalog)
        if path.endswith("/plan"):
            return httpx.Response(200, json=plan_body)
        pid = path.rsplit("/", 1)[-1]
        return httpx.Response(200 if pid in known else 404, json={"id": pid})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://admin-cp")


def make_billing_client(
    *, summary_body=None, timeseries_body=None, by_user_body=None, by_key_body=None, unavailable=False
):
    """A stub billing internal client via MockTransport, routed by exact path:
    - GET /internal/v1/usage/summary: 200 with `summary_body` (defaults to a zeroed shape).
    - GET /internal/v1/usage/timeseries: 200 with `timeseries_body` (defaults to an empty list
      of buckets).
    `unavailable=True` raises a transport error to exercise the fail-closed (502) path for both.
    Every request the handler sees (path + query params) is appended to `.calls` so a test can
    assert on exactly what was forwarded — needed for the "org_id scoped"/"start,end passed
    through"/"start,end omitted when absent" scenarios (add-org-cp-usage-proxy)."""
    summary = summary_body if summary_body is not None else {
        "requests": 0, "total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0,
        "period": {"start": "2026-01-01", "end": "2026-02-01"},
    }
    timeseries = timeseries_body if timeseries_body is not None else {
        "period": {"start": "2026-01-01", "end": "2026-02-01"}, "buckets": [],
    }
    by_user = by_user_body if by_user_body is not None else {
        "period": {"start": "2026-01-01", "end": "2026-02-01"}, "users": [],
    }
    by_key = by_key_body if by_key_body is not None else {
        "period": {"start": "2026-01-01", "end": "2026-02-01"}, "api_keys": [],
    }
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"path": request.url.path, "params": dict(request.url.params)})
        if unavailable:
            raise httpx.ConnectError("billing down", request=request)
        path = request.url.path
        if path == "/internal/v1/usage/summary":
            return httpx.Response(200, json=summary)
        if path == "/internal/v1/usage/timeseries":
            return httpx.Response(200, json=timeseries)
        if path == "/internal/v1/usage/by-user":
            return httpx.Response(200, json=by_user)
        if path == "/internal/v1/usage/by-key":
            return httpx.Response(200, json=by_key)
        return httpx.Response(404, json={"error": "not found"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://billing")
    client.calls = calls  # type: ignore[attr-defined]
    return client


def make_client(*, engine=None, admin_client=None, billing_client=None, **settings_kwargs):
    engine = engine or make_engine()
    admin_client = admin_client if admin_client is not None else make_admin_client()
    billing_client = billing_client if billing_client is not None else make_billing_client()
    kwargs = {
        "service_api_key": SERVICE_KEY,
        "mg_service_api_key": MG_KEY,
        "database_url": "sqlite+aiosqlite://",
    }
    kwargs.update(settings_kwargs)
    app = create_app(
        Settings(**kwargs), engine=engine, admin_client=admin_client, billing_client=billing_client
    )
    return TestClient(app, raise_server_exceptions=False), engine


def auth_headers(key: str = SERVICE_KEY) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def org_headers(org_id, key: str = SERVICE_KEY) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "X-Organization-Id": str(org_id)}


def mg_headers(key: str = MG_KEY) -> dict[str, str]:
    """Bearer for the model-gateway's OWN call site (require_mg_service_key) — distinct from the
    broad `auth_headers()`/SERVICE_KEY."""
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def client():
    c, _engine = make_client()
    with c:
        yield c


# ── Local-auth (modules/auth) test support ───────────────────────────────────────────────────
# Endpoint tests seed a user WITH a password_hash (the /v1 API cannot), so they drive the app over
# an in-loop ASGI transport (httpx.ASGITransport) instead of TestClient — schema create + seeding +
# requests all share ONE event loop, sidestepping aiosqlite's per-loop connection binding.

import httpx as _httpx  # noqa: E402

JWT_SECRET = "test-jwt-secret-0123456789abcdef0123456789"


def make_auth_app(*, engine=None, admin_client=None, **settings_kwargs):
    """Build the app for auth endpoint tests with a jwt_secret set (no TestClient/lifespan)."""
    engine = engine or make_engine()
    admin_client = admin_client if admin_client is not None else make_admin_client()
    kwargs = {
        "service_api_key": SERVICE_KEY,
        "mg_service_api_key": MG_KEY,
        "database_url": "sqlite+aiosqlite://",
        "jwt_secret": JWT_SECRET,
    }
    kwargs.update(settings_kwargs)
    app = create_app(Settings(**kwargs), engine=engine, admin_client=admin_client)
    return app, engine


async def init_schema(engine):
    from db import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def asgi_client(app) -> _httpx.AsyncClient:
    # https base URL so the Secure refresh cookie is sent back on /refresh and /logout.
    return _httpx.AsyncClient(
        transport=_httpx.ASGITransport(app=app), base_url="https://testserver"
    )


async def seed_org(sessionmaker, org_id=None, *, status="active", name="Acme"):
    from modules.organizations.models import Organization

    org_id = org_id or uuid.uuid4()
    async with sessionmaker() as s:
        if await s.get(Organization, org_id) is None:
            s.add(Organization(id=org_id, name=name, status=status))
            await s.commit()
    return org_id


async def seed_user(
    sessionmaker,
    *,
    org_id=None,
    email="user@acme.com",
    username="user",
    password="Sup3rSecret!!",
    status="active",
    must_change_password=False,
    org_status="active",
    with_hash=True,
):
    """Seed an Organization (if needed) + a User with an Argon2id password_hash. Returns
    (user_id, org_id, password)."""
    from modules.auth.password import hash_password
    from modules.organizations.models import Organization
    from modules.users.models import User

    org_id = org_id or uuid.uuid4()
    async with sessionmaker() as s:
        if await s.get(Organization, org_id) is None:
            s.add(Organization(id=org_id, name="Acme", status=org_status))
        u = User(
            organization_id=org_id,
            username=username,
            email=email,
            status=status,
            must_change_password=must_change_password,
            password_hash=hash_password(password) if with_hash else None,
        )
        s.add(u)
        await s.commit()
        uid = u.id
    return uid, org_id, password


@pytest.fixture
def fake_redis(monkeypatch):
    """Patch modules.auth.lockout.get_redis to return one shared fakeredis client, so lockout
    counters persist across attempts within a test."""
    import fakeredis.aioredis

    from modules.auth import lockout

    fake = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(lockout, "get_redis", lambda settings: fake)
    return fake
