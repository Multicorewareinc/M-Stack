"""Audit logs (Org CP): events are recorded for user / role / API-key / auth actions, and the read
surfaces enforce scope — /api/audit-logs is the caller's own org only, /internal/v1/audit-logs spans
every org. Drives the app over httpx.ASGITransport on one event loop (like test_api_keys.py)."""

from __future__ import annotations

import uuid

import fakeredis.aioredis
import pytest
from pydantic import SecretStr

from conftest import (
    JWT_SECRET,
    SERVICE_KEY,
    asgi_client,
    init_schema,
    make_admin_client,
    make_auth_app,
    org_headers,
    seed_org,
    seed_user,
)

from modules.api_keys import cache
from modules.auth.jwt import encode_access_token

# API-key routes are gated per action (apikey.create / .update / .delete).
PERM_IDS = {"create": uuid.uuid4(), "update": uuid.uuid4(), "delete": uuid.uuid4()}
PERM_ID = PERM_IDS["create"]  # any known catalog id, for role-permission composition
CATALOG = [
    {
        "id": str(pid),
        "resource": "apikey",
        "action": action,
        "slug": f"apikey.{action}",
        "description": None,
        "is_active": True,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    for action, pid in PERM_IDS.items()
]


@pytest.fixture(autouse=True)
def _fake_valkey(monkeypatch):
    # revoke/rotate evict from the verify cache; keep that off a real Valkey.
    monkeypatch.setattr(cache, "get_redis", lambda settings: fakeredis.aioredis.FakeRedis())


def _app():
    admin = make_admin_client(known_permission_ids=list(PERM_IDS.values()), permission_catalog=CATALOG)
    return make_auth_app(admin_client=admin)


def _jwt(uid, org_id, email="user@acme.com") -> dict:
    token = encode_access_token(
        sub=str(uid), email=email, org=str(org_id), secret=SecretStr(JWT_SECRET), ttl_seconds=3600
    )
    return {"Authorization": f"Bearer {token}"}


async def _grant_manage(sessionmaker, uid, org_id):
    from modules.roles.models import Role, RolePermission, UserRole

    async with sessionmaker() as s:
        role = Role(organization_id=org_id, name=f"key-admin-{uuid.uuid4().hex[:6]}")
        s.add(role)
        await s.flush()
        for pid in PERM_IDS.values():
            s.add(RolePermission(role_id=role.id, permission_id=pid))
        s.add(UserRole(user_id=uid, role_id=role.id))
        await s.commit()


async def _own_logs(c, uid, org_id, **params):
    r = await c.get("/api/audit-logs", params=params, headers=_jwt(uid, org_id))
    assert r.status_code == 200, r.text
    return r.json()


async def test_api_key_lifecycle_is_audited():
    app, engine = _app()
    await init_schema(engine)
    uid, org_id, _ = await seed_user(app.state.sessionmaker)
    await _grant_manage(app.state.sessionmaker, uid, org_id)
    h = _jwt(uid, org_id)
    async with asgi_client(app) as c:
        created = (await c.post("/api/api-keys", json={"name": "ci"}, headers=h)).json()
        assert (await c.post(f"/api/api-keys/{created['id']}/rotate", headers=h)).status_code == 200
        assert (await c.delete(f"/api/api-keys/{created['id']}", headers=h)).status_code == 204
        page = await _own_logs(c, uid, org_id)

    actions = [i["action"] for i in page["items"]]
    assert actions == ["api_key.revoked", "api_key.rotated", "api_key.created"]  # newest first
    first = page["items"][-1]
    assert first["actor_email"] == "user@acme.com" and first["actor_type"] == "user"
    assert first["resource_name"] == "ci" and first["organization_id"] == str(org_id)
    assert page["total"] == 3
    assert "raw_key" not in str(page)  # never leaks the secret


async def test_org_portal_sees_only_its_own_org():
    app, engine = _app()
    await init_schema(engine)
    sm = app.state.sessionmaker
    uid_a, org_a, _ = await seed_user(sm, email="a@acme.com", username="a")
    uid_b, org_b, _ = await seed_user(sm, email="b@globex.com", username="b")
    await _grant_manage(sm, uid_a, org_a)
    await _grant_manage(sm, uid_b, org_b)
    async with asgi_client(app) as c:
        await c.post("/api/api-keys", json={"name": "a-key"}, headers=_jwt(uid_a, org_a, "a@acme.com"))
        await c.post("/api/api-keys", json={"name": "b-key"}, headers=_jwt(uid_b, org_b, "b@globex.com"))
        a = await _own_logs(c, uid_a, org_a)
        b = await _own_logs(c, uid_b, org_b)

    assert [i["resource_name"] for i in a["items"]] == ["a-key"]
    assert [i["resource_name"] for i in b["items"]] == ["b-key"]


async def test_org_endpoint_requires_a_user_jwt():
    app, engine = _app()
    await init_schema(engine)
    async with asgi_client(app) as c:
        assert (await c.get("/api/audit-logs")).status_code == 401
        # The broad service bearer is NOT an org identity — it must not open the tenant surface.
        r = await c.get("/api/audit-logs", headers={"Authorization": f"Bearer {SERVICE_KEY}"})
        assert r.status_code == 401


async def test_internal_endpoint_spans_all_orgs_and_filters_by_org():
    app, engine = _app()
    await init_schema(engine)
    sm = app.state.sessionmaker
    uid_a, org_a, _ = await seed_user(sm, email="a@acme.com", username="a")
    uid_b, org_b, _ = await seed_user(sm, email="b@globex.com", username="b")
    await _grant_manage(sm, uid_a, org_a)
    await _grant_manage(sm, uid_b, org_b)
    svc = {"Authorization": f"Bearer {SERVICE_KEY}"}
    async with asgi_client(app) as c:
        await c.post("/api/api-keys", json={"name": "a-key"}, headers=_jwt(uid_a, org_a, "a@acme.com"))
        await c.post("/api/api-keys", json={"name": "b-key"}, headers=_jwt(uid_b, org_b, "b@globex.com"))
        everything = (await c.get("/internal/v1/audit-logs", headers=svc)).json()
        only_b = (await c.get("/internal/v1/audit-logs", params={"organization_id": str(org_b)}, headers=svc)).json()
        no_key = await c.get("/internal/v1/audit-logs")

    assert {i["resource_name"] for i in everything["items"]} == {"a-key", "b-key"}
    assert [i["resource_name"] for i in only_b["items"]] == ["b-key"]
    assert no_key.status_code == 401


async def test_user_and_role_changes_are_audited_with_service_and_admin_actors():
    app, engine = _app()
    await init_schema(engine)
    org_id = await seed_org(app.state.sessionmaker)
    async with asgi_client(app) as c:
        # Plain service-key caller (no actor headers) → "service".
        r = await c.post(
            "/v1/users",
            json={"username": "carol", "email": "carol@acme.com"},
            headers=org_headers(org_id),
        )
        assert r.status_code == 201, r.text
        carol = r.json()["id"]
        # Admin CP forwards the acting super-admin → "admin".
        headers = {**org_headers(org_id), "X-Actor-Email": "root@platform.io", "X-Actor-Id": "abc"}
        r = await c.patch(f"/v1/users/{carol}", json={"status": "suspended"}, headers=headers)
        assert r.status_code == 200, r.text
        r = await c.post("/v1/roles", json={"name": "auditors"}, headers=headers)
        assert r.status_code == 201, r.text
        assert (await c.delete(f"/v1/users/{carol}", headers=headers)).status_code == 204
        page = (
            await c.get(
                "/internal/v1/audit-logs",
                params={"organization_id": str(org_id)},
                headers={"Authorization": f"Bearer {SERVICE_KEY}"},
            )
        ).json()

    by_action = {i["action"]: i for i in page["items"]}
    assert set(by_action) == {"user.created", "user.updated", "role.created", "user.deleted"}
    assert by_action["user.created"]["actor_type"] == "service"
    assert by_action["user.updated"]["actor_type"] == "admin"
    assert by_action["user.updated"]["actor_email"] == "root@platform.io"
    assert by_action["user.updated"]["details"] == {"changed": ["status"], "status": "suspended"}
    assert by_action["user.deleted"]["resource_name"] == "carol@acme.com"


async def test_actor_headers_are_ignored_for_a_jwt_caller():
    """X-Actor-* is trusted only under the service bearer; a JWT caller can't spoof another identity."""
    app, engine = _app()
    await init_schema(engine)
    sm = app.state.sessionmaker
    uid, org_id, _ = await seed_user(sm)
    await _grant_manage(sm, uid, org_id)
    async with asgi_client(app) as c:
        await c.post(
            "/api/api-keys",
            json={"name": "k"},
            headers={**_jwt(uid, org_id), "X-Actor-Email": "evil@x.io"},
        )
        page = await _own_logs(c, uid, org_id)
    assert page["items"][0]["actor_email"] == "user@acme.com"
    assert page["items"][0]["actor_type"] == "user"


async def test_login_success_and_failure_are_audited_without_secrets(fake_redis):
    app, engine = _app()
    await init_schema(engine)
    sm = app.state.sessionmaker
    uid, org_id, password = await seed_user(sm)
    async with asgi_client(app) as c:
        bad = await c.post("/api/auth/login", json={"email": "user@acme.com", "password": "wrong-password-1"})
        assert bad.status_code == 401
        ok = await c.post("/api/auth/login", json={"email": "user@acme.com", "password": password})
        assert ok.status_code == 200, ok.text
        page = await _own_logs(c, uid, org_id)

    assert [i["action"] for i in page["items"]] == ["auth.login", "auth.login_failed"]
    assert password not in str(page) and "wrong-password-1" not in str(page)


async def test_filters_search_and_pagination():
    app, engine = _app()
    await init_schema(engine)
    sm = app.state.sessionmaker
    uid, org_id, _ = await seed_user(sm)
    await _grant_manage(sm, uid, org_id)
    h = _jwt(uid, org_id)
    async with asgi_client(app) as c:
        for name in ("alpha", "beta", "gamma"):
            await c.post("/api/api-keys", json={"name": name}, headers=h)
        by_action = await _own_logs(c, uid, org_id, action="api_key.created")
        by_search = await _own_logs(c, uid, org_id, search="bet")
        page1 = await _own_logs(c, uid, org_id, limit=2, offset=0)
        page2 = await _own_logs(c, uid, org_id, limit=2, offset=2)
        bad_limit = await c.get("/api/audit-logs", params={"limit": 0}, headers=h)

    assert by_action["total"] == 3
    assert [i["resource_name"] for i in by_search["items"]] == ["beta"]
    assert [i["resource_name"] for i in page1["items"]] == ["gamma", "beta"]
    assert [i["resource_name"] for i in page2["items"]] == ["alpha"]
    assert page1["total"] == page2["total"] == 3
    assert bad_limit.status_code == 422


async def test_role_changes_and_role_assignment_are_audited():
    app, engine = _app()
    await init_schema(engine)
    org_id = await seed_org(app.state.sessionmaker)
    h = org_headers(org_id)
    async with asgi_client(app) as c:
        user = (await c.post("/v1/users", json={"username": "dan", "email": "dan@acme.com"}, headers=h)).json()
        role = (await c.post("/v1/roles", json={"name": "ops"}, headers=h)).json()
        assert (await c.patch(f"/v1/roles/{role['id']}", json={"description": "x"}, headers=h)).status_code == 200
        r = await c.put(f"/v1/roles/{role['id']}/permissions", json={"permission_ids": [str(PERM_ID)]}, headers=h)
        assert r.status_code == 200, r.text
        r = await c.put(f"/v1/users/{user['id']}/roles", json={"role_ids": [role["id"]]}, headers=h)
        assert r.status_code == 200, r.text
        assert (await c.get(f"/v1/users/{user['id']}/roles", headers=h)).status_code == 200  # reads aren't audited
        assert (await c.delete(f"/v1/roles/{role['id']}", headers=h)).status_code == 204
        page = (
            await c.get(
                "/internal/v1/audit-logs",
                params={"organization_id": str(org_id)},
                headers={"Authorization": f"Bearer {SERVICE_KEY}"},
            )
        ).json()

    assert [i["action"] for i in page["items"]] == [
        "role.deleted",
        "user.roles_updated",
        "role.permissions_updated",
        "role.updated",
        "role.created",
        "user.created",
    ]
    by_action = {i["action"]: i for i in page["items"]}
    assert by_action["role.deleted"]["resource_name"] == "ops"
    assert by_action["user.roles_updated"]["resource_name"] == "dan@acme.com"
    assert by_action["role.permissions_updated"]["details"] == {"permission_count": 1}
