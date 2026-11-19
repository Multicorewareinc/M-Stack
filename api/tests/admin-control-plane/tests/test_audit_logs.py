"""Audit logs (Admin CP): platform events (orgs, plans, permissions, admin auth) are recorded, and
GET /v1/audit-logs returns them merged, newest-first, with Org CP's cross-org feed (users, roles,
API keys). Requires super-admin auth."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from conftest import (
    ADMIN_KEY,
    asgi_client,
    auth_headers,
    init_schema,
    make_auth_app,
    make_client,
    make_org_client,
    seed_admin,
)

H = auth_headers()


def _org_item(action, org_id, minutes_ago, **extra):
    return {
        "id": str(uuid.uuid4()),
        "organization_id": str(org_id),
        "actor_type": "user",
        "actor_id": str(uuid.uuid4()),
        "actor_email": "usera@acme.com",
        "action": action,
        "resource_type": "api_key",
        "resource_id": None,
        "resource_name": "ci",
        "details": {},
        "ip_address": None,
        "created_at": (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(),
        **extra,
    }


def test_requires_admin_auth():
    c, _ = make_client()
    with c:
        assert c.get("/v1/audit-logs").status_code == 401
        assert c.get("/v1/audit-logs", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_platform_events_are_recorded_and_listed():
    c, _ = make_client()
    with c:
        plan = c.post("/v1/plans", json={"name": "Team"}, headers=H).json()
        c.patch(f"/v1/plans/{plan['id']}", json={"rpm": 5}, headers=H)
        org = c.post("/v1/organizations", json={"name": "Acme", "plan_id": plan["id"]}, headers=H).json()
        other = c.post("/v1/plans", json={"name": "Scale"}, headers=H).json()
        c.patch(f"/v1/organizations/{org['id']}", json={"plan_id": other["id"]}, headers=H)
        c.patch(f"/v1/organizations/{org['id']}", json={"status": "suspended"}, headers=H)
        c.delete(f"/v1/organizations/{org['id']}", headers=H)
        body = c.get("/v1/audit-logs", params={"limit": 50}, headers=H).json()

    actions = [i["action"] for i in body["items"]]
    assert actions == [
        "organization.deleted",
        "organization.status_changed",
        "organization.plan_changed",
        "plan.created",  # "Scale"
        "organization.created",
        "plan.updated",
        "plan.created",  # "Team"
    ]
    assert body["total"] == 7
    created = next(i for i in body["items"] if i["action"] == "organization.created")
    assert created["organization_id"] == org["id"] and created["resource_name"] == "Acme"
    assert created["actor_type"] == "service"  # the static admin key, not a super-admin JWT
    assert created["source"] == "platform"
    changed = next(i for i in body["items"] if i["action"] == "organization.status_changed")
    assert changed["details"] == {"from": "active", "to": "suspended"}


def test_merges_org_cp_events_across_all_orgs_newest_first():
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    feed = [
        _org_item("api_key.created", org_a, minutes_ago=10),
        _org_item("api_key.revoked", org_b, minutes_ago=1),
        _org_item("user.created", org_b, minutes_ago=30),
    ]
    c, _ = make_client(org_client=make_org_client(audit_items=feed))
    with c:
        plan = c.post("/v1/plans", json={"name": "Team"}, headers=H).json()  # local event, "now"
        body = c.get("/v1/audit-logs", headers=H).json()
        page2 = c.get("/v1/audit-logs", params={"limit": 2, "offset": 2}, headers=H).json()

    assert [i["action"] for i in body["items"]] == [
        "plan.created",  # newest: just now
        "api_key.revoked",  # 1 min ago
        "api_key.created",  # 10 min ago
        "user.created",  # 30 min ago
    ]
    assert body["total"] == 4
    assert {i["source"] for i in body["items"] if i["action"] != "plan.created"} == {"organization"}
    assert {i["organization_id"] for i in body["items"] if i["organization_id"]} == {str(org_a), str(org_b)}
    # Pagination slices the merged feed, not each side separately.
    assert [i["action"] for i in page2["items"]] == ["api_key.created", "user.created"]
    assert page2["total"] == 4 and plan["name"] == "Team"


def test_forwards_filters_to_org_cp():
    seen: list = []
    c, _ = make_client(org_client=make_org_client(recorder=seen))
    org_id = uuid.uuid4()
    with c:
        r = c.get(
            "/v1/audit-logs",
            params={"organization_id": str(org_id), "action": "api_key.created", "search": "ci", "limit": 10, "offset": 5},
            headers=H,
        )
    assert r.status_code == 200
    forwarded = next(q for q in seen if q.url.path == "/internal/v1/audit-logs")
    p = dict(forwarded.url.params)
    assert p["organization_id"] == str(org_id) and p["action"] == "api_key.created" and p["search"] == "ci"
    assert p["limit"] == "15" and p["offset"] == "0"  # each side supplies its newest offset+limit rows


def test_org_cp_outage_is_a_502_not_a_silently_partial_log():
    c, _ = make_client(org_client=make_org_client(unavailable=True))
    with c:
        assert c.get("/v1/audit-logs", headers=H).status_code == 502


def test_rejects_paging_past_the_merge_window_and_bad_limits():
    c, _ = make_client()
    with c:
        assert c.get("/v1/audit-logs", params={"limit": 200, "offset": 4900}, headers=H).status_code == 422
        assert c.get("/v1/audit-logs", params={"limit": 0}, headers=H).status_code == 422
        assert c.get("/v1/audit-logs", params={"limit": 201}, headers=H).status_code == 422


async def test_admin_login_and_failed_login_are_audited(fake_redis):
    app, engine = make_auth_app()
    await init_schema(engine)
    _uid, password = await seed_admin(app.state.sessionmaker)
    async with asgi_client(app) as c:
        bad = await c.post("/api/auth/login", json={"email": "admin@platform.local", "password": "wrong-password-1"})
        ok = await c.post("/api/auth/login", json={"email": "admin@platform.local", "password": password})
        assert (bad.status_code, ok.status_code) == (401, 200)
        token = ok.json()["access_token"]
        body = (await c.get("/v1/audit-logs", headers={"Authorization": f"Bearer {token}"})).json()

    assert [i["action"] for i in body["items"]] == ["auth.login", "auth.login_failed"]
    assert body["items"][0]["actor_type"] == "admin"
    assert body["items"][0]["actor_email"] == "admin@platform.local"
    assert password not in str(body) and "wrong-password-1" not in str(body)


def test_super_admin_provisioning_forwards_actor_to_org_cp(fake_redis):
    """Users created through the admin portal are logged by Org CP — they must carry WHO did it."""
    import asyncio

    seen: list = []

    async def go():
        app, engine = make_auth_app(org_client=make_org_client(recorder=seen))
        await init_schema(engine)
        uid, password = await seed_admin(app.state.sessionmaker)
        async with asgi_client(app) as c:
            token = (await c.post("/api/auth/login", json={"email": "admin@platform.local", "password": password})).json()["access_token"]
            h = {"Authorization": f"Bearer {token}"}
            plan = (await c.post("/v1/plans", json={"name": "Team"}, headers=h)).json()
            org = (await c.post("/v1/organizations", json={"name": "Acme", "plan_id": plan["id"]}, headers=h)).json()
            r = await c.post(
                f"/v1/organizations/{org['id']}/users",
                json={"username": "own", "email": "own@acme.com", "password": "Sup3rSecret!!"},
                headers=h,
            )
            assert r.status_code == 201, r.text
        return str(uid)

    uid = asyncio.run(go())

    posts = [q for q in seen if q.method == "POST" and q.url.path == "/v1/users"]
    assert posts and posts[0].headers["X-Actor-Email"] == "admin@platform.local"
    assert posts[0].headers["X-Actor-Id"] == uid
