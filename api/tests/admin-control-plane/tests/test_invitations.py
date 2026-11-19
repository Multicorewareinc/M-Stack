"""Super-admin invitations (Admin CP -> Org CP proxy). An invitation targets an EXISTING organization or
a NEW one created on the spot; the Org CP (stubbed here via make_org_client) owns the invitation itself."""

from __future__ import annotations

import json
import uuid

from conftest import asgi_client, init_schema, make_auth_app, make_org_client, seed_admin

EMAIL = "new.person@example.com"


async def _app(**org_kwargs):
    recorder = []
    app, engine = make_auth_app(org_client=make_org_client(recorder=recorder, **org_kwargs))
    await init_schema(engine)
    return app, recorder


async def _seed_plan(sm):
    from modules.plans.models import Plan

    async with sm() as s:
        p = Plan(name="Free", tpm=1, rpm=1, quota_monthly_tokens=1, is_default=True)
        s.add(p)
        await s.commit()
        return p.id


async def _seed_org(sm, plan_id, name="Acme"):
    from modules.organizations.models import Organization
    from modules.organizations.schemas import slugify

    async with sm() as s:
        o = Organization(name=name, slug=slugify(name), plan_id=plan_id, status="active")
        s.add(o)
        await s.commit()
        return o.id


async def _token(app):
    uid, pw = await seed_admin(app.state.sessionmaker)
    async with asgi_client(app) as c:
        r = await c.post("/api/auth/login", json={"email": "admin@platform.local", "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _invites(recorder):
    return [rq for rq in recorder if rq.url.path.endswith("/invitations") and rq.method == "POST"]


async def test_invites_into_an_existing_org_with_the_chosen_role(fake_redis):
    app, recorder = await _app()
    sm = app.state.sessionmaker
    org_id = await _seed_org(sm, await _seed_plan(sm))
    h = await _token(app)

    async with asgi_client(app) as c:
        r = await c.post(
            "/v1/invitations",
            json={"email": EMAIL, "name": "New Person", "role": "org_admin", "organization_id": str(org_id)},
            headers=h,
        )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["organization_created"] is False and body["invite_path"].startswith("/accept-invite?token=")
    sent = _invites(recorder)
    assert len(sent) == 1 and sent[0].url.path == f"/internal/v1/organizations/{org_id}/invitations"
    assert json.loads(sent[0].content) == {"email": EMAIL, "name": "New Person", "role_name": "org_admin"}
    # attributed to the acting super-admin so Org CP's audit log shows who invited
    assert sent[0].headers["x-actor-email"] == "admin@platform.local"
    # no org was created
    assert not [rq for rq in recorder if rq.url.path == "/internal/v1/organizations" and rq.method == "POST"]


async def test_invites_into_a_brand_new_org(fake_redis):
    app, recorder = await _app()
    sm = app.state.sessionmaker
    plan_id = await _seed_plan(sm)
    h = await _token(app)

    async with asgi_client(app) as c:
        r = await c.post(
            "/v1/invitations",
            json={
                "email": EMAIL,
                "name": "New Person",
                "role": "org_admin",
                "new_organization": {"name": "NewCo", "plan_id": str(plan_id)},
            },
            headers=h,
        )
        orgs = await c.get("/v1/organizations", headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["organization_created"] is True
    inits = [rq for rq in recorder if rq.url.path == "/internal/v1/organizations" and rq.method == "POST"]
    assert len(inits) == 1  # the org was provisioned on Org CP first
    new_org = next(o for o in orgs.json()["items"] if o["name"] == "NewCo") if isinstance(orgs.json(), dict) else next(
        o for o in orgs.json() if o["name"] == "NewCo"
    )
    assert new_org["status"] == "active"
    assert _invites(recorder)[0].url.path == f"/internal/v1/organizations/{new_org['id']}/invitations"


async def test_exactly_one_target_is_required(fake_redis):
    app, recorder = await _app()
    sm = app.state.sessionmaker
    plan_id = await _seed_plan(sm)
    org_id = await _seed_org(sm, plan_id)
    h = await _token(app)
    async with asgi_client(app) as c:
        neither = await c.post("/v1/invitations", json={"email": EMAIL}, headers=h)
        both = await c.post(
            "/v1/invitations",
            json={
                "email": EMAIL,
                "organization_id": str(org_id),
                "new_organization": {"name": "X", "plan_id": str(plan_id)},
            },
            headers=h,
        )
        bad_role = await c.post(
            "/v1/invitations", json={"email": EMAIL, "role": "superuser", "organization_id": str(org_id)}, headers=h
        )
    assert neither.status_code == both.status_code == bad_role.status_code == 422
    assert not _invites(recorder)


async def test_unknown_org_is_404_and_nothing_is_sent(fake_redis):
    app, recorder = await _app()
    h = await _token(app)
    async with asgi_client(app) as c:
        r = await c.post("/v1/invitations", json={"email": EMAIL, "organization_id": str(uuid.uuid4())}, headers=h)
    assert r.status_code == 404 and not _invites(recorder)


async def test_an_email_that_already_has_an_account_is_a_409_on_the_email_field(fake_redis):
    app, recorder = await _app(invite_status=409)
    sm = app.state.sessionmaker
    org_id = await _seed_org(sm, await _seed_plan(sm))
    h = await _token(app)
    async with asgi_client(app) as c:
        r = await c.post("/v1/invitations", json={"email": EMAIL, "organization_id": str(org_id)}, headers=h)
    assert r.status_code == 409 and r.json()["error"]["field"] == "email"


async def test_requires_a_super_admin(fake_redis):
    app, _ = await _app()
    async with asgi_client(app) as c:
        anon = await c.post("/v1/invitations", json={"email": EMAIL, "organization_id": str(uuid.uuid4())})
        junk = await c.post(
            "/v1/invitations",
            json={"email": EMAIL, "organization_id": str(uuid.uuid4())},
            headers={"Authorization": "Bearer nope"},
        )
    assert anon.status_code == 401 and junk.status_code == 401


async def test_org_cp_outage_is_a_502(fake_redis):
    app, _ = await _app()
    sm = app.state.sessionmaker
    org_id = await _seed_org(sm, await _seed_plan(sm))
    h = await _token(app)
    # swap in a dead Org CP after the admin is logged in
    app.state.org_client = make_org_client(unavailable=True)
    async with asgi_client(app) as c:
        r = await c.post("/v1/invitations", json={"email": EMAIL, "organization_id": str(org_id)}, headers=h)
    assert r.status_code == 502


# ------------------------------------------------------------------ list + revoke


def _row(email, org_id):
    return {
        "id": str(uuid.uuid4()), "email": email, "name": None, "organization_id": str(org_id),
        "role_id": str(uuid.uuid4()), "role_name": "org_user", "invited_by": "root@platform.local",
        "created_at": "2026-06-01T00:00:00Z", "expires_at": "2026-06-08T00:00:00Z",
    }


async def test_lists_pending_invitations_and_forwards_the_filters(fake_redis):
    org_id = uuid.uuid4()
    rows = [_row("a@x.co", org_id), _row("b@x.co", org_id)]
    app, recorder = await _app(invitations=rows)
    h = await _token(app)
    async with asgi_client(app) as c:
        r = await c.get(
            "/v1/invitations",
            params={"organization_id": str(org_id), "search": "a@", "role": "org_user", "limit": 10, "offset": 5},
            headers=h,
        )
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 2 and [i["email"] for i in r.json()["items"]] == ["a@x.co", "b@x.co"]
    sent = next(rq for rq in recorder if rq.url.path == "/internal/v1/invitations")
    assert dict(sent.url.params) == {
        "organization_id": str(org_id), "search": "a@", "role": "org_user", "limit": "10", "offset": "5",
    }


async def test_revokes_an_invitation_as_the_acting_super_admin(fake_redis):
    app, recorder = await _app()
    h = await _token(app)
    inv_id = uuid.uuid4()
    async with asgi_client(app) as c:
        r = await c.delete(f"/v1/invitations/{inv_id}", headers=h)
    assert r.status_code == 204
    sent = next(rq for rq in recorder if rq.method == "DELETE")
    assert sent.url.path == f"/internal/v1/invitations/{inv_id}"
    assert sent.headers["x-actor-email"] == "admin@platform.local"


async def test_revoke_maps_org_cp_errors(fake_redis):
    for upstream, expected in ((404, 404), (409, 409), (500, 502)):
        app, _ = await _app(revoke_status=upstream)
        h = await _token(app)
        async with asgi_client(app) as c:
            r = await c.delete(f"/v1/invitations/{uuid.uuid4()}", headers=h)
        assert r.status_code == expected, (upstream, r.status_code, r.text)


async def test_listing_and_revoking_need_a_super_admin_and_a_reachable_org_cp(fake_redis):
    app, _ = await _app()
    async with asgi_client(app) as c:
        assert (await c.get("/v1/invitations")).status_code == 401
        assert (await c.delete(f"/v1/invitations/{uuid.uuid4()}")).status_code == 401
    h = await _token(app)
    app.state.org_client = make_org_client(unavailable=True)
    async with asgi_client(app) as c:
        assert (await c.get("/v1/invitations", headers=h)).status_code == 502
        assert (await c.delete(f"/v1/invitations/{uuid.uuid4()}", headers=h)).status_code == 502



BAD_EMAILS = ["a..b@example.com", "x@y.1", "<script>@x.io", "jane@example", "a@-bad-.com", "a b@example.com",
              "jane@example.com\nBcc:x@y.co", "a" * 65 + "@example.com"]


async def test_a_malformed_email_is_refused_before_a_new_organization_is_created(fake_redis):
    app, recorder = await _app()
    sm = app.state.sessionmaker
    plan_id = await _seed_plan(sm)
    h = await _token(app)
    async with asgi_client(app) as c:
        for bad in BAD_EMAILS:
            r = await c.post(
                "/v1/invitations",
                json={"email": bad, "role": "org_admin", "new_organization": {"name": "GhostCo", "plan_id": str(plan_id)}},
                headers=h,
            )
            assert r.status_code == 422, bad
        orgs = await c.get("/v1/organizations", headers=h)
    # nothing reached the Org CP: no organization was provisioned and no invitation was attempted
    assert not [rq for rq in recorder if rq.url.path == "/internal/v1/organizations" and rq.method == "POST"]
    assert not _invites(recorder)
    body = orgs.json()
    items = body["items"] if isinstance(body, dict) else body
    assert all(o["name"] != "GhostCo" for o in items)


async def test_a_well_formed_email_is_normalised_and_forwarded(fake_redis):
    app, recorder = await _app()
    sm = app.state.sessionmaker
    org_id = await _seed_org(sm, await _seed_plan(sm))
    h = await _token(app)
    async with asgi_client(app) as c:
        r = await c.post(
            "/v1/invitations", json={"email": "  Jane.Doe+Tag@Example.COM ", "organization_id": str(org_id)}, headers=h
        )
    assert r.status_code == 201, r.text
    assert json.loads(_invites(recorder)[0].content)["email"] == "jane.doe+tag@example.com"


async def test_issuing_waits_long_enough_for_the_org_cp_to_send_the_email(fake_redis):
    from modules.invitations.service import ISSUE_TIMEOUT_SECONDS

    app, recorder = await _app()
    sm = app.state.sessionmaker
    org_id = await _seed_org(sm, await _seed_plan(sm))
    h = await _token(app)
    async with asgi_client(app) as c:
        r = await c.post("/v1/invitations", json={"email": EMAIL, "organization_id": str(org_id)}, headers=h)
    assert r.status_code == 201
    sent = _invites(recorder)[0]
    # the Org CP emails before it answers (several seconds over SMTP), so this call must not use the 3 s
    # default the client has for quick reads
    assert sent.extensions["timeout"]["read"] == ISSUE_TIMEOUT_SECONDS >= 20


async def test_a_timeout_is_reported_as_slow_not_as_unavailable(fake_redis):
    import httpx

    app, _ = await _app()
    sm = app.state.sessionmaker
    org_id = await _seed_org(sm, await _seed_plan(sm))
    h = await _token(app)

    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    app.state.org_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://org-cp")
    async with asgi_client(app) as c:
        r = await c.post("/v1/invitations", json={"email": EMAIL, "organization_id": str(org_id)}, headers=h)
    assert r.status_code == 502
    msg = r.json()["error"]["message"]
    assert "took too long" in msg and "may have been created" in msg  # not the misleading "unavailable"
