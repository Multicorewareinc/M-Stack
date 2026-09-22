"""Super-admin user provisioning (ADR-029/AD-02b,AD-02c, SP-03) — Admin CP -> Org CP proxy routes.
Uses the asgi auth harness (single event loop) so admin seeding, login, and the gated proxy calls
share one loop; the Org CP is stubbed via make_org_client (routes + a recorder)."""

from __future__ import annotations

import uuid

from conftest import (
    asgi_client,
    init_schema,
    make_auth_app,
    make_org_client,
    seed_admin,
)

PW = "Sup3rSecret!!x"
OWNER = {"username": "owner", "email": "owner@acme.com", "password": "Own3rPass!!x"}


async def _app(org_client=None):
    app, engine = make_auth_app(org_client=org_client)
    await init_schema(engine)
    return app


async def _seed_plan(sm, name="Free"):
    from modules.plans.models import Plan

    async with sm() as s:
        p = Plan(name=name, tpm=1, rpm=1, quota_monthly_tokens=1, is_default=True)
        s.add(p)
        await s.commit()
        return p.id


async def _seed_org(sm, plan_id, name="Acme", status="active"):
    from modules.organizations.models import Organization
    from modules.organizations.schemas import slugify

    async with sm() as s:
        o = Organization(name=name, slug=slugify(name), plan_id=plan_id, status=status)
        s.add(o)
        await s.commit()
        return o.id


async def _super_admin_token(app, *, must_change=False):
    uid, pw = await seed_admin(app.state.sessionmaker, must_change_password=must_change)
    async with asgi_client(app) as c:
        r = await c.post("/api/auth/login", json={"email": "admin@platform.local", "password": pw})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def test_super_admin_creates_user_for_existing_org(fake_redis):
    import json as _json

    recorder = []
    org_admin_role_id = str(uuid.uuid4())
    app = await _app(org_client=make_org_client(recorder=recorder, org_admin_role_id=org_admin_role_id))
    sm = app.state.sessionmaker
    plan_id = await _seed_plan(sm)
    org_id = await _seed_org(sm, plan_id)
    token = await _super_admin_token(app)

    async with asgi_client(app) as c:
        r = await c.post(f"/v1/organizations/{org_id}/users", json=OWNER, headers=_bearer(token))
    assert r.status_code == 201, r.text
    # The user create was proxied under the correct org (from the path), with a password (login-capable).
    posts = [rq for rq in recorder if rq.url.path == "/v1/users" and rq.method == "POST"]
    assert len(posts) == 1
    assert posts[0].headers["x-organization-id"] == str(org_id)
    assert b"password" in posts[0].content
    # org_admin (not org_user, and not some other role) was the role actually bound.
    assigns = [rq for rq in recorder if rq.url.path.endswith("/roles") and rq.method == "PUT"]
    assert len(assigns) == 1 and assigns[0].headers["x-organization-id"] == str(org_id)
    assert _json.loads(assigns[0].content) == {"role_ids": [org_admin_role_id]}


async def test_create_user_for_unknown_org_404(fake_redis):
    recorder = []
    app = await _app(org_client=make_org_client(recorder=recorder))
    token = await _super_admin_token(app)
    async with asgi_client(app) as c:
        r = await c.post(
            f"/v1/organizations/{uuid.uuid4()}/users", json=OWNER, headers=_bearer(token)
        )
    assert r.status_code == 404
    # No Org CP user-create attempted for a non-existent org.
    assert not [rq for rq in recorder if rq.url.path == "/v1/users"]


async def test_create_org_with_owner(fake_redis):
    import json as _json

    recorder = []
    org_admin_role_id = str(uuid.uuid4())
    created_user = {
        "id": str(uuid.uuid4()), "email": OWNER["email"], "username": OWNER["username"],
        "organization_id": str(uuid.uuid4()),
    }
    app = await _app(org_client=make_org_client(
        recorder=recorder, org_admin_role_id=org_admin_role_id, created_user=created_user,
    ))
    sm = app.state.sessionmaker
    plan_id = await _seed_plan(sm)
    token = await _super_admin_token(app)

    async with asgi_client(app) as c:
        r = await c.post(
            "/v1/organizations/with-owner",
            json={"name": "NewCo", "plan_id": str(plan_id), "owner": OWNER},
            headers=_bearer(token),
        )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["organization"]["name"] == "NewCo" and body["organization"]["status"] == "active"
    # The response actually carries the created owner Org CP returned, not just a truthy "owner" key.
    assert body["owner"]["id"] == created_user["id"]
    assert body["owner"]["email"] == OWNER["email"]
    # The workflow initialized the org on Org CP, created the owner WITH a password, and bound
    # org_admin specifically (not org_user or some other role) to that owner.
    inits = [rq for rq in recorder if rq.url.path == "/internal/v1/organizations" and rq.method == "POST"]
    assert len(inits) == 1
    creates = [rq for rq in recorder if rq.url.path == "/v1/users" and rq.method == "POST"]
    assert len(creates) == 1
    created_body = _json.loads(creates[0].content)
    assert created_body["username"] == OWNER["username"] and created_body["email"] == OWNER["email"]
    assert created_body["password"] == OWNER["password"]
    assigns = [rq for rq in recorder if rq.url.path.endswith("/roles") and rq.method == "PUT"]
    assert len(assigns) == 1
    assert assigns[0].url.path == f"/v1/users/{created_user['id']}/roles"
    assert _json.loads(assigns[0].content) == {"role_ids": [org_admin_role_id]}


async def test_create_org_with_owner_unknown_plan_422(fake_redis):
    app = await _app(org_client=make_org_client())
    token = await _super_admin_token(app)
    async with asgi_client(app) as c:
        r = await c.post(
            "/v1/organizations/with-owner",
            json={"name": "NewCo", "plan_id": str(uuid.uuid4()), "owner": OWNER},
            headers=_bearer(token),
        )
    assert r.status_code == 422


async def test_create_org_with_owner_duplicate_name_409(fake_redis):
    app = await _app(org_client=make_org_client())
    sm = app.state.sessionmaker
    plan_id = await _seed_plan(sm)
    await _seed_org(sm, plan_id, name="Twin")
    token = await _super_admin_token(app)
    async with asgi_client(app) as c:
        r = await c.post(
            "/v1/organizations/with-owner",
            json={"name": "Twin", "plan_id": str(plan_id), "owner": OWNER},
            headers=_bearer(token),
        )
    assert r.status_code == 409


async def test_create_org_with_owner_duplicate_owner_email_409(fake_redis):
    """A conflict from Org CP's own POST /v1/users (duplicate owner email within that org) must
    propagate as 409, not a raw 5xx or a silently-ignored failure."""
    app = await _app(org_client=make_org_client(create_user_status=409))
    sm = app.state.sessionmaker
    plan_id = await _seed_plan(sm)
    token = await _super_admin_token(app)
    async with asgi_client(app) as c:
        r = await c.post(
            "/v1/organizations/with-owner",
            json={"name": "NewCo2", "plan_id": str(plan_id), "owner": OWNER},
            headers=_bearer(token),
        )
    assert r.status_code == 409


async def test_provisioning_requires_super_admin_token(fake_redis):
    app = await _app()
    sm = app.state.sessionmaker
    plan_id = await _seed_plan(sm)
    org_id = await _seed_org(sm, plan_id)
    async with asgi_client(app) as c:
        no_token = await c.post(f"/v1/organizations/{org_id}/users", json=OWNER)
        bad = await c.post(
            f"/v1/organizations/{org_id}/users", json=OWNER, headers=_bearer("not-a-jwt")
        )
    assert no_token.status_code == 401
    assert bad.status_code == 401


async def test_provisioning_rejects_org_tier_token(fake_redis):
    """A valid, well-signed Org CP token (wrong tier — an org_user, not a super-admin) must be
    rejected, not accidentally accepted because the two CPs share one HS256 secret. Since ADR-025's
    `aud`/`iss` fix, this is caught at decode time (InvalidAudienceError -> 401) before Admin CP
    even attempts the admin_users lookup."""
    import jwt as pyjwt

    from conftest import JWT_SECRET

    app = await _app()
    sm = app.state.sessionmaker
    plan_id = await _seed_plan(sm)
    org_id = await _seed_org(sm, plan_id)
    org_tier_token = pyjwt.encode(
        {
            "sub": str(uuid.uuid4()), "email": "u@acme.com", "role": "org_user",
            "org": str(uuid.uuid4()), "iss": "org-control-plane", "aud": "org-control-plane",
            "iat": 0, "exp": 9999999999, "jti": "x",
        },
        JWT_SECRET,
        algorithm="HS256",
    )
    async with asgi_client(app) as c:
        r = await c.post(
            f"/v1/organizations/{org_id}/users", json=OWNER, headers=_bearer(org_tier_token)
        )
    assert r.status_code == 401


async def test_unrotated_super_admin_cannot_provision(fake_redis):
    app = await _app()
    sm = app.state.sessionmaker
    plan_id = await _seed_plan(sm)
    token = await _super_admin_token(app, must_change=True)
    async with asgi_client(app) as c:
        r = await c.post(
            "/v1/organizations/with-owner",
            json={"name": "NewCo", "plan_id": str(plan_id), "owner": OWNER},
            headers=_bearer(token),
        )
    assert r.status_code == 403  # PASSWORD_CHANGE_REQUIRED


async def test_duplicate_email_surfaces_409(fake_redis):
    app = await _app(org_client=make_org_client(create_user_status=409))
    sm = app.state.sessionmaker
    plan_id = await _seed_plan(sm)
    org_id = await _seed_org(sm, plan_id)
    token = await _super_admin_token(app)
    async with asgi_client(app) as c:
        r = await c.post(f"/v1/organizations/{org_id}/users", json=OWNER, headers=_bearer(token))
    assert r.status_code == 409


async def test_org_cp_unavailable_surfaces_upstream_error(fake_redis):
    app = await _app(org_client=make_org_client(unavailable=True))
    sm = app.state.sessionmaker
    plan_id = await _seed_plan(sm)
    org_id = await _seed_org(sm, plan_id)
    token = await _super_admin_token(app)
    async with asgi_client(app) as c:
        r = await c.post(f"/v1/organizations/{org_id}/users", json=OWNER, headers=_bearer(token))
    assert r.status_code >= 500  # upstream error, never a 2xx
