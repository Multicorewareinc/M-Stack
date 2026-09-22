"""Test helpers: build the app with an injected in-memory aiosqlite engine, so tests never
touch Postgres (mirrors the gateway's/rate-limiter's injected-client pattern, adapted to a DB).

Two gotchas that otherwise make every test 500:
- `StaticPool` keeps the single in-memory connection alive so the schema + seeded rows persist
  across the app's connections within a test (a fresh in-memory sqlite per connection is empty).
- The `TestClient` MUST be entered as a context manager (`with client:`) so the app's lifespan
  startup runs — that is what creates the schema (create_all on the injected engine) and seeds.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from main import create_app
from settings import Settings

ADMIN_KEY = "test-admin-key"
# Distinct from ADMIN_KEY on purpose — the security fix under test is that the super-admin bearer
# and the Org CP-facing service bearer are two separate secrets (see dependencies.py). A test that
# accidentally reused ADMIN_KEY here would silently defeat that check.
SERVICE_KEY = "test-admin-cp-service-key"


def make_engine():
    """A fresh in-memory aiosqlite engine whose single connection persists for the test."""
    return create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)


def make_org_client(
    status_code=201,
    *,
    unavailable=False,
    recorder=None,
    user_counts=None,
    create_user_status=201,
    created_user=None,
    org_admin_role_id=None,
    permission_in_use=False,
):
    """A stub Org CP internal client via MockTransport for the org-creation workflow, the
    organizations-list/detail user-count enrichment, AND (SP-03) the super-admin user-provisioning
    proxy (`POST /v1/users`, `GET /v1/roles`, `PUT /v1/users/{id}/roles`) — all routed by path.
    Default init returns 201; default counts are empty. `unavailable=True` raises a transport error
    for every call except user-counts (fail-closed path). `recorder` (a list) captures request
    objects so tests can assert what was sent. `create_user_status` overrides the `POST /v1/users`
    status (e.g. 409 for a duplicate); `created_user` is the JSON returned on a 201 create;
    `org_admin_role_id` is the id returned for the `org_admin` role from `GET /v1/roles`;
    `permission_in_use` controls the GET /internal/v1/roles/permission-usage stub used by
    delete_permission's reverse-reference check (council review fix)."""
    counts = user_counts if user_counts is not None else {}
    created_user = created_user if created_user is not None else {
        "id": str(uuid.uuid4()), "email": "owner@acme.com", "username": "owner",
        "organization_id": str(uuid.uuid4()),
    }
    org_admin_role_id = str(org_admin_role_id) if org_admin_role_id is not None else str(uuid.uuid4())
    org_user_role_id = str(uuid.uuid4())

    def handler(request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        # Routed independent of `unavailable`/`status_code`, which only ever describe the
        # org-creation init call — a test simulating "Org CP is down" for creation still needs
        # to list organizations afterward to see the persisted `failed` status.
        if path == "/internal/v1/organizations/user-counts":
            return httpx.Response(200, json={"counts": counts})
        # Permission reverse-reference check (council review fix) — default "not in use" so
        # existing create/delete-permission tests keep passing unless a test opts into
        # `permission_in_use=True`, or `permission_in_use="unavailable"` to exercise the
        # fail-closed 502 path independent of the org-creation `unavailable` flag.
        if path == "/internal/v1/roles/permission-usage":
            if permission_in_use == "unavailable":
                raise httpx.ConnectError("org cp down", request=request)
            return httpx.Response(200, json={"in_use": bool(permission_in_use)})
        if unavailable:
            raise httpx.ConnectError("org cp down", request=request)
        if recorder is not None:
            recorder.append(request)
        # SP-03 super-admin provisioning proxy routes.
        if path == "/v1/users" and method == "POST":
            if create_user_status == 201:
                return httpx.Response(201, json=created_user)
            return httpx.Response(create_user_status, json={"error": {"type": "conflict"}})
        if path == "/v1/roles" and method == "GET":
            return httpx.Response(200, json=[
                {"id": org_admin_role_id, "name": "org_admin"},
                {"id": org_user_role_id, "name": "org_user"},
            ])
        if path.endswith("/roles") and method == "PUT":
            return httpx.Response(200, json={"role_ids": [org_admin_role_id]})
        # Default = the org-creation init call.
        return httpx.Response(status_code, json={"id": "stub", "name": "stub"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://org-cp")


def make_billing_client(status_code=200, *, unavailable=False, recorder=None, outcome="created"):
    """A stub billing client via MockTransport (add-billing-plan-subscription-linkage) — mirrors
    `make_org_client`'s shape. `recorder` (a list) captures request objects so tests can assert
    what was sent. `unavailable=True` raises a transport error for every call (billing-outage
    scenarios). Default: every `POST /internal/v1/subscriptions` call succeeds with `outcome`."""

    def handler(request: httpx.Request) -> httpx.Response:
        if unavailable:
            raise httpx.ConnectError("billing down", request=request)
        if recorder is not None:
            recorder.append(request)
        return httpx.Response(
            status_code, json={"outcome": outcome, "stripe_subscription_id": "sub_stub"}
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://billing")


def make_client(*, engine=None, org_client=None, billing_client=None, **settings_kwargs):
    """Build the app over an injected aiosqlite engine. Returns (TestClient, engine).

    A default success Org CP stub (and a default success billing stub) is injected so
    organization-creation tests (and existing org CRUD tests) succeed without a live Org CP or
    billing. Passing the same `engine` back in simulates a reboot against an existing database
    (used by the seed-idempotency test)."""
    engine = engine or make_engine()
    org_client = org_client if org_client is not None else make_org_client()
    billing_client = billing_client if billing_client is not None else make_billing_client()
    kwargs = {
        "admin_api_key": ADMIN_KEY,
        "service_api_key": SERVICE_KEY,
        "database_url": "sqlite+aiosqlite://",
    }
    kwargs.update(settings_kwargs)
    app = create_app(Settings(**kwargs), engine=engine, org_client=org_client, billing_client=billing_client)
    return TestClient(app, raise_server_exceptions=False), engine


def auth_headers(key: str = ADMIN_KEY) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def service_headers(key: str = SERVICE_KEY) -> dict[str, str]:
    """Bearer for this service's OWN /internal/v1/* routes (require_service_key) — distinct from
    the super-admin `auth_headers()` (require_admin_key)."""
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def client():
    """A ready-to-use client with schema created and the three default plans seeded."""
    c, _engine = make_client()
    with c:
        yield c


@pytest.fixture
def plan_id(client):
    """The id of a seeded plan (Free), handy for attaching organizations."""
    plans = client.get("/v1/plans", headers=auth_headers()).json()
    return next(p["id"] for p in plans if p["name"] == "Free")


# ── Local-auth (modules/auth) test support ───────────────────────────────────────────────────
# Endpoint tests seed an admin_users row WITH a password_hash (no API can), so they drive the app
# over an in-loop ASGI transport (httpx.ASGITransport) instead of TestClient — schema create +
# seeding + requests all share ONE event loop, sidestepping aiosqlite's per-loop connection binding.

import uuid  # noqa: E402

JWT_SECRET = "test-jwt-secret-0123456789abcdef0123456789"


def make_auth_app(*, engine=None, org_client=None, billing_client=None, **settings_kwargs):
    """Build the app for auth endpoint tests with a jwt_secret set (no TestClient/lifespan)."""
    engine = engine or make_engine()
    org_client = org_client if org_client is not None else make_org_client()
    billing_client = billing_client if billing_client is not None else make_billing_client()
    kwargs = {
        "admin_api_key": ADMIN_KEY,
        "service_api_key": SERVICE_KEY,
        "database_url": "sqlite+aiosqlite://",
        "jwt_secret": JWT_SECRET,
    }
    kwargs.update(settings_kwargs)
    app = create_app(Settings(**kwargs), engine=engine, org_client=org_client, billing_client=billing_client)
    return app, engine


async def init_schema(engine):
    from db import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def asgi_client(app) -> httpx.AsyncClient:
    # https base URL so the Secure refresh cookie is sent back on /refresh and /logout.
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    )


async def seed_admin(
    sessionmaker,
    *,
    email="admin@platform.local",
    password="Sup3rSecret!!",
    active=True,
    must_change_password=False,
    with_hash=True,
):
    """Seed an AdminUser with an Argon2id password_hash (email stored lowercased). Returns
    (user_id, password)."""
    from modules.auth.models import AdminUser
    from modules.auth.password import hash_password

    async with sessionmaker() as s:
        u = AdminUser(
            email=email.lower(),
            active=active,
            must_change_password=must_change_password,
            password_hash=hash_password(password) if with_hash else None,
        )
        s.add(u)
        await s.commit()
        uid = u.id
    return uid, password


@pytest.fixture
def fake_redis(monkeypatch):
    """Patch modules.auth.lockout.get_redis to return one shared fakeredis client, so lockout
    counters persist across attempts within a test."""
    import fakeredis.aioredis

    from modules.auth import lockout

    fake = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(lockout, "get_redis", lambda settings: fake)
    return fake
