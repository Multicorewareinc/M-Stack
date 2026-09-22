"""Endpoint: POST /api/users — identity + RBAC-gated org-admin user provisioning (ADR-029/AD-01,
AD-02a, SP-02). Uses the asgi auth harness so org-init (seeds default roles), user seeding, login,
and the gated create all share one event loop.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import SecretStr

from conftest import (
    JWT_SECRET,
    asgi_client,
    auth_headers,
    init_schema,
    make_admin_client,
    make_auth_app,
    org_headers,
    seed_user,
)
from modules.auth.jwt import encode_access_token

PW = "Sup3rSecret!!x"
NEW_PW = "N3wUserPass!!x"
H = auth_headers()


async def _app(**kw):
    app, engine = make_auth_app(**kw)
    await init_schema(engine)
    return app, engine


async def _init_org(app, org_id, name="Acme"):
    async with asgi_client(app) as c:
        r = await c.post(
            "/internal/v1/organizations", json={"id": str(org_id), "name": name}, headers=H
        )
        assert r.status_code in (200, 201), r.text


async def _role_id(app, org_id, role_name):
    async with asgi_client(app) as c:
        roles = (await c.get("/v1/roles", headers=org_headers(org_id))).json()
    return next(r["id"] for r in roles if r["name"] == role_name)


async def _provision_actor(app, org_id, role_name, *, must_change=False):
    """Init org (seeds default roles), seed an actor with a password, assign `role_name`. Returns uid."""
    await _init_org(app, org_id)
    uid, _, _ = await seed_user(
        app.state.sessionmaker,
        org_id=org_id,
        email="admin@acme.com",
        username="admin",
        password=PW,
        must_change_password=must_change,
    )
    rid = await _role_id(app, org_id, role_name)
    async with asgi_client(app) as c:
        r = await c.put(f"/v1/users/{uid}/roles", json={"role_ids": [rid]}, headers=org_headers(org_id))
        assert r.status_code == 200, r.text
    return uid


async def _login(app, email, pw):
    async with asgi_client(app) as c:
        return await c.post("/api/auth/login", json={"email": email, "password": pw})


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def test_org_admin_creates_user_who_can_login(fake_redis):
    app, _ = await _app()
    org_id = uuid.uuid4()
    await _provision_actor(app, org_id, "org_admin")
    token = (await _login(app, "admin@acme.com", PW)).json()["access_token"]

    async with asgi_client(app) as c:
        r = await c.post(
            "/api/users",
            json={"username": "newbie", "email": "newbie@acme.com", "password": NEW_PW},
            headers=_bearer(token),
        )
    assert r.status_code == 201, r.text
    assert r.json()["organization_id"] == str(org_id)

    # The created user can immediately authenticate.
    login = await _login(app, "newbie@acme.com", NEW_PW)
    assert login.status_code == 200, login.text
    assert login.json()["must_change_password"] is True


async def test_created_user_gets_org_user_role(fake_redis):
    app, _ = await _app()
    org_id = uuid.uuid4()
    await _provision_actor(app, org_id, "org_admin")
    token = (await _login(app, "admin@acme.com", PW)).json()["access_token"]
    org_user_role = await _role_id(app, org_id, "org_user")

    async with asgi_client(app) as c:
        created = await c.post(
            "/api/users",
            json={"username": "newbie", "email": "newbie@acme.com", "password": NEW_PW},
            headers=_bearer(token),
        )
        assert created.status_code == 201
        new_id = created.json()["id"]
        roles = (await c.get(f"/v1/users/{new_id}/roles", headers=org_headers(org_id))).json()
    assert org_user_role in roles["role_ids"]


async def test_org_from_token_not_header(fake_redis):
    app, _ = await _app()
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    await _provision_actor(app, org_a, "org_admin")
    await _init_org(app, org_b, name="OtherCorp")
    token = (await _login(app, "admin@acme.com", PW)).json()["access_token"]

    async with asgi_client(app) as c:
        r = await c.post(
            "/api/users",
            json={"username": "newbie", "email": "newbie@acme.com", "password": NEW_PW},
            headers={**_bearer(token), "X-Organization-Id": str(org_b)},  # attacker-supplied
        )
        assert r.status_code == 201
        assert r.json()["organization_id"] == str(org_a)  # token's org, not the header's
        # Not present in org B.
        b_users = (await c.get("/v1/users", headers=org_headers(org_b))).json()
    assert all(u["email"] != "newbie@acme.com" for u in b_users)


async def test_create_duplicate_email_conflicts(fake_redis):
    app, _ = await _app()
    org_id = uuid.uuid4()
    await _provision_actor(app, org_id, "org_admin")
    token = (await _login(app, "admin@acme.com", PW)).json()["access_token"]
    body = {"username": "dup", "email": "dup@acme.com", "password": NEW_PW}
    async with asgi_client(app) as c:
        assert (await c.post("/api/users", json=body, headers=_bearer(token))).status_code == 201
        second = await c.post(
            "/api/users",
            json={"username": "dup2", "email": "dup@acme.com", "password": NEW_PW},
            headers=_bearer(token),
        )
    assert second.status_code == 409


async def test_create_denied_without_users_create(fake_redis):
    app, _ = await _app()
    org_id = uuid.uuid4()
    await _provision_actor(app, org_id, "org_user")  # plain org_user lacks users.create
    token = (await _login(app, "admin@acme.com", PW)).json()["access_token"]
    async with asgi_client(app) as c:
        r = await c.post(
            "/api/users",
            json={"username": "newbie", "email": "newbie@acme.com", "password": NEW_PW},
            headers=_bearer(token),
        )
        assert r.status_code == 403
        users = (await c.get("/v1/users", headers=org_headers(org_id))).json()
    assert all(u["email"] != "newbie@acme.com" for u in users)  # nothing created


async def test_create_rejects_missing_and_orgless_token(fake_redis):
    app, _ = await _app()
    org_id = uuid.uuid4()
    uid = await _provision_actor(app, org_id, "org_admin")
    body = {"username": "newbie", "email": "newbie@acme.com", "password": NEW_PW}
    # Org-less token for the same real user (no `org` claim) — a member-less principal.
    orgless = encode_access_token(
        sub=str(uid), email="admin@acme.com", org=None, secret=SecretStr(JWT_SECRET), ttl_seconds=3600
    )
    async with asgi_client(app) as c:
        no_token = await c.post("/api/users", json=body)
        org_less = await c.post("/api/users", json=body, headers=_bearer(orgless))
    assert no_token.status_code == 401
    assert org_less.status_code == 403  # ORG_REQUIRED


async def test_unrotated_actor_cannot_create(fake_redis):
    app, _ = await _app()
    org_id = uuid.uuid4()
    await _provision_actor(app, org_id, "org_admin", must_change=True)
    token = (await _login(app, "admin@acme.com", PW)).json()["access_token"]
    async with asgi_client(app) as c:
        r = await c.post(
            "/api/users",
            json={"username": "newbie", "email": "newbie@acme.com", "password": NEW_PW},
            headers=_bearer(token),
        )
    assert r.status_code == 403  # PASSWORD_CHANGE_REQUIRED — confined to /api/auth/password


async def test_create_fails_closed_when_permission_unresolved(fake_redis):
    # Provision + login on an app with an available Admin CP; then attempt the create on a second app
    # (shared engine) whose Admin CP is unreachable, so users.create cannot be resolved -> 403.
    app_ok, engine = await _app()
    org_id = uuid.uuid4()
    await _provision_actor(app_ok, org_id, "org_admin")
    token = (await _login(app_ok, "admin@acme.com", PW)).json()["access_token"]

    app_bad, _ = make_auth_app(engine=engine, admin_client=make_admin_client(unavailable=True))
    async with asgi_client(app_bad) as c:
        r = await c.post(
            "/api/users",
            json={"username": "newbie", "email": "newbie@acme.com", "password": NEW_PW},
            headers=_bearer(token),
        )
    assert r.status_code == 403
