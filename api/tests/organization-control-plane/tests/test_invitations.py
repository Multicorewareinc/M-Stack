"""Invitations (Org CP): an org admin (JWT + users.create) or Admin CP (service bearer) issues a
one-time link; the invitee opens it on a public page, chooses their own password, and a user with the
invited role is created. Covers the token lifecycle (replace / expire / spend), tenant isolation of the
role, the audit trail, and that the invitee can then actually log in."""

from __future__ import annotations

import uuid

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from conftest import (
    JWT_SECRET,
    asgi_client,
    auth_headers,
    init_schema,
    make_auth_app,
    seed_org,
    seed_user,
)

from modules.auth.jwt import encode_access_token
from modules.invitations.models import Invitation
from modules.roles.models import Role, RolePermission, UserRole
from modules.users.models import User

USERS_CREATE_ID = uuid.uuid5(uuid.NAMESPACE_DNS, "users.create")  # the stub catalog's id for the slug
PASSWORD = "Chosen-By-Invitee-42!"


def _jwt(uid, org_id, email="admin@acme.com") -> dict:
    token = encode_access_token(
        sub=str(uid), email=email, org=str(org_id), secret=SecretStr(JWT_SECRET), ttl_seconds=3600
    )
    return {"Authorization": f"Bearer {token}"}


async def _seed_roles(sessionmaker, org_id, *, grant_users_create_to=None):
    """The org's default roles. org_admin carries users.create; org_user does not."""
    async with sessionmaker() as s:
        admin = Role(organization_id=org_id, name="org_admin", is_system_role=True)
        member = Role(organization_id=org_id, name="org_user", is_system_role=True)
        s.add_all([admin, member])
        await s.flush()
        s.add(RolePermission(role_id=admin.id, permission_id=USERS_CREATE_ID))
        if grant_users_create_to is not None:
            s.add(UserRole(user_id=grant_users_create_to, role_id=admin.id))
        await s.commit()
        return admin.id, member.id


async def _setup(**settings):
    app, engine = make_auth_app(**settings)
    await init_schema(engine)
    return app, app.state.sessionmaker


def _token_from(body: dict) -> str:
    return body["invite_path"].split("token=", 1)[1]


async def _invite_internal(c, org_id, **body):
    body.setdefault("email", "new.person@example.com")
    body.setdefault("name", "New Person")
    return await c.post(
        f"/internal/v1/organizations/{org_id}/invitations", json=body, headers=auth_headers()
    )


async def test_admin_cp_issues_an_invitation_and_the_invitee_can_accept_and_log_in(fake_redis):
    app, sm = await _setup(org_portal_base_url="https://org.example.com/")
    org_id = await seed_org(sm, name="Globex")
    admin_id, member_id = await _seed_roles(sm, org_id)

    async with asgi_client(app) as c:
        r = await _invite_internal(c, org_id, role_name="org_admin")
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["organization_name"] == "Globex" and body["role_name"] == "org_admin"
        assert body["email"] == "new.person@example.com"
        assert body["invite_url"] == "https://org.example.com" + body["invite_path"]
        token = _token_from(body)

        seen = await c.post("/api/invitations/inspect", json={"token": token})
        assert seen.status_code == 200
        assert seen.json()["organization_name"] == "Globex" and seen.json()["role_name"] == "org_admin"

        done = await c.post("/api/invitations/accept", json={"token": token, "password": PASSWORD})
        assert done.status_code == 204, done.text

        login = await c.post("/api/auth/login", json={"email": "new.person@example.com", "password": PASSWORD})
        assert login.status_code == 200, login.text
        assert login.json()["must_change_password"] is False  # they chose it themselves

    async with sm() as s:
        user = await s.scalar(select(User).where(User.email == "new.person@example.com"))
        assert user.organization_id == org_id and user.display_name == "New Person"
        assert (user.first_name, user.last_name) == ("New", "Person")
        roles = list(await s.scalars(select(UserRole.role_id).where(UserRole.user_id == user.id)))
        assert roles == [admin_id]


async def test_only_the_hash_is_stored_and_the_link_is_single_use(fake_redis):
    app, sm = await _setup()
    org_id = await seed_org(sm)
    await _seed_roles(sm, org_id)
    async with asgi_client(app) as c:
        token = _token_from((await _invite_internal(c, org_id)).json())
        async with sm() as s:
            inv = await s.scalar(select(Invitation))
            assert token not in (inv.token_hash, inv.email) and len(inv.token_hash) == 64

        assert (await c.post("/api/invitations/accept", json={"token": token, "password": PASSWORD})).status_code == 204
        again = await c.post("/api/invitations/accept", json={"token": token, "password": PASSWORD})
        assert again.status_code == 410
        assert (await c.post("/api/invitations/inspect", json={"token": token})).status_code == 410


async def test_unknown_token_is_a_404_and_a_weak_password_leaves_the_link_usable():
    app, sm = await _setup()
    org_id = await seed_org(sm)
    await _seed_roles(sm, org_id)
    async with asgi_client(app) as c:
        assert (await c.post("/api/invitations/inspect", json={"token": "x" * 43})).status_code == 404
        token = _token_from((await _invite_internal(c, org_id)).json())
        weak = await c.post("/api/invitations/accept", json={"token": token, "password": "short"})
        assert weak.status_code == 422
        assert (await c.post("/api/invitations/inspect", json={"token": token})).status_code == 200
    async with sm() as s:
        assert await s.scalar(select(User).where(User.email == "new.person@example.com")) is None


async def test_a_newer_invitation_replaces_the_pending_one():
    app, sm = await _setup()
    org_id = await seed_org(sm)
    await _seed_roles(sm, org_id)
    async with asgi_client(app) as c:
        first = _token_from((await _invite_internal(c, org_id)).json())
        second = _token_from((await _invite_internal(c, org_id)).json())
        assert first != second
        assert (await c.post("/api/invitations/inspect", json={"token": first})).status_code == 410
        assert (await c.post("/api/invitations/inspect", json={"token": second})).status_code == 200


async def test_an_expired_invitation_is_gone():
    app, sm = await _setup(invitation_ttl_hours=0)
    org_id = await seed_org(sm)
    await _seed_roles(sm, org_id)
    async with asgi_client(app) as c:
        token = _token_from((await _invite_internal(c, org_id)).json())
        r = await c.post("/api/invitations/accept", json={"token": token, "password": PASSWORD})
        assert r.status_code == 410 and "expired" in r.json()["error"]["message"]


async def test_cannot_invite_an_email_that_already_has_an_account():
    app, sm = await _setup()
    org_id = await seed_org(sm)
    await _seed_roles(sm, org_id)
    await seed_user(sm, org_id=org_id, email="new.person@example.com", username="np")
    async with asgi_client(app) as c:
        r = await _invite_internal(c, org_id)
        assert r.status_code == 409 and r.json()["error"]["field"] == "email"


async def test_defaults_to_org_user_and_rejects_unknown_org_or_role():
    app, sm = await _setup()
    org_id = await seed_org(sm)
    _, member_id = await _seed_roles(sm, org_id)
    async with asgi_client(app) as c:
        ok = (await _invite_internal(c, org_id)).json()
        assert ok["role_name"] == "org_user" and ok["role_id"] == str(member_id)
        assert (await _invite_internal(c, org_id, role_name="nope")).status_code == 422
        assert (await _invite_internal(c, uuid.uuid4())).status_code == 422
        bad_email = await _invite_internal(c, org_id, email="not-an-email")
        assert bad_email.status_code == 422


async def test_internal_route_needs_the_service_bearer():
    app, sm = await _setup()
    org_id = await seed_org(sm)
    async with asgi_client(app) as c:
        r = await c.post(f"/internal/v1/organizations/{org_id}/invitations", json={"email": "a@b.co"})
        assert r.status_code == 401


async def test_org_admin_invites_into_their_own_org_only():
    app, sm = await _setup()
    uid, org_a, _ = await seed_user(sm, email="admin@acme.com", username="admin")
    admin_a, member_a = await _seed_roles(sm, org_a, grant_users_create_to=uid)
    org_b = await seed_org(sm, name="Other")
    admin_b, _ = await _seed_roles(sm, org_b)
    h = _jwt(uid, org_a)
    async with asgi_client(app) as c:
        ok = await c.post("/api/invitations", json={"email": "x@y.co", "name": "X", "role_id": str(member_a)}, headers=h)
        assert ok.status_code == 201, ok.text
        assert ok.json()["organization_id"] == str(org_a) and ok.json()["role_name"] == "org_user"
        # a role id from another tenant is refused, never silently accepted
        cross = await c.post("/api/invitations", json={"email": "z@y.co", "role_id": str(admin_b)}, headers=h)
        assert cross.status_code == 422
        # no token / service bearer is not an org identity
        assert (await c.post("/api/invitations", json={"email": "q@y.co"})).status_code == 401


async def test_inviting_requires_the_users_create_permission():
    app, sm = await _setup()
    uid, org_id, _ = await seed_user(sm, email="member@acme.com", username="member")
    _, member_id = await _seed_roles(sm, org_id)
    async with sm() as s:
        s.add(UserRole(user_id=uid, role_id=member_id))
        await s.commit()
    async with asgi_client(app) as c:
        r = await c.post("/api/invitations", json={"email": "x@y.co"}, headers=_jwt(uid, org_id, "member@acme.com"))
        assert r.status_code == 403


async def test_username_collisions_get_a_numeric_suffix(fake_redis):
    app, sm = await _setup()
    org_id = await seed_org(sm)
    await _seed_roles(sm, org_id)
    await seed_user(sm, org_id=org_id, email="someone@else.com", username="new.person")
    async with asgi_client(app) as c:
        token = _token_from((await _invite_internal(c, org_id)).json())
        assert (await c.post("/api/invitations/accept", json={"token": token, "password": PASSWORD})).status_code == 204
    async with sm() as s:
        user = await s.scalar(select(User).where(User.email == "new.person@example.com"))
        assert user.username == "new.person2"


async def test_creating_and_accepting_are_audited_without_the_token(fake_redis):
    app, sm = await _setup()
    uid, org_id, _ = await seed_user(sm, email="admin@acme.com", username="admin")
    admin_id, _ = await _seed_roles(sm, org_id, grant_users_create_to=uid)
    h = _jwt(uid, org_id)
    async with asgi_client(app) as c:
        body = (await c.post("/api/invitations", json={"email": "x@y.co", "role_id": str(admin_id)}, headers=h)).json()
        token = _token_from(body)
        await c.post("/api/invitations/accept", json={"token": token, "password": PASSWORD})
        logs = (await c.get("/api/audit-logs", headers=h)).json()["items"]
    actions = {i["action"]: i for i in logs}
    assert actions["invitation.created"]["actor_email"] == "admin@acme.com"
    assert actions["invitation.created"]["details"] == {"role": "org_admin"}
    assert actions["invitation.accepted"]["actor_email"] == "x@y.co"
    assert token not in str(logs)


# ------------------------------------------------------------------ list + revoke


async def _issue(c, org_id, email, *, role_name=None, delay=False):
    body = {"email": email, "name": email.split("@")[0]}
    if role_name:
        body["role_name"] = role_name
    r = await c.post(f"/internal/v1/organizations/{org_id}/invitations", json=body, headers=auth_headers())
    assert r.status_code == 201, r.text
    return r.json()


async def test_org_admin_lists_only_their_own_orgs_pending_invitations_newest_first():
    app, sm = await _setup()
    uid, org_a, _ = await seed_user(sm, email="admin@acme.com", username="admin")
    await _seed_roles(sm, org_a, grant_users_create_to=uid)
    org_b = await seed_org(sm, name="Other")
    await _seed_roles(sm, org_b)
    h = _jwt(uid, org_a)
    async with asgi_client(app) as c:
        await _issue(c, org_a, "first@a.co")
        await _issue(c, org_a, "second@a.co", role_name="org_admin")
        await _issue(c, org_b, "theirs@b.co")  # another tenant -- must never show up
        r = await c.get("/api/invitations", headers=h)
    assert r.status_code == 200, r.text
    page = r.json()
    assert page["total"] == 2 and page["limit"] == 25 and page["offset"] == 0
    assert [i["email"] for i in page["items"]] == ["second@a.co", "first@a.co"]  # newest first
    assert [i["role_name"] for i in page["items"]] == ["org_admin", "org_user"]
    assert all(i["organization_id"] == str(org_a) for i in page["items"])
    assert "token" not in str(page) and "hash" not in str(page)  # never leaks the secret


async def test_list_filters_by_email_and_role_and_paginates():
    app, sm = await _setup()
    uid, org_id, _ = await seed_user(sm, email="admin@acme.com", username="admin")
    await _seed_roles(sm, org_id, grant_users_create_to=uid)
    h = _jwt(uid, org_id)
    async with asgi_client(app) as c:
        for e in ("alice@x.co", "bob@x.co", "carol_ann@x.co"):
            await _issue(c, org_id, e)
        await _issue(c, org_id, "dave@x.co", role_name="org_admin")
        by_email = (await c.get("/api/invitations", params={"search": "AL"}, headers=h)).json()
        by_role = (await c.get("/api/invitations", params={"role": "org_admin"}, headers=h)).json()
        literal = (await c.get("/api/invitations", params={"search": "_ann"}, headers=h)).json()
        wildcard = (await c.get("/api/invitations", params={"search": "%"}, headers=h)).json()
        p1 = (await c.get("/api/invitations", params={"limit": 3, "offset": 0}, headers=h)).json()
        p2 = (await c.get("/api/invitations", params={"limit": 3, "offset": 3}, headers=h)).json()
        bad = await c.get("/api/invitations", params={"limit": 0}, headers=h)
    assert [i["email"] for i in by_email["items"]] == ["alice@x.co"]
    assert [i["email"] for i in by_role["items"]] == ["dave@x.co"]
    assert [i["email"] for i in literal["items"]] == ["carol_ann@x.co"]  # "_" is a literal, not a wildcard
    assert wildcard["total"] == 0  # "%" is a literal too
    assert p1["total"] == p2["total"] == 4 and len(p1["items"]) == 3 and len(p2["items"]) == 1
    assert bad.status_code == 422


async def test_accepted_and_replaced_invitations_drop_out_of_the_list(fake_redis):
    app, sm = await _setup()
    uid, org_id, _ = await seed_user(sm, email="admin@acme.com", username="admin")
    await _seed_roles(sm, org_id, grant_users_create_to=uid)
    h = _jwt(uid, org_id)
    async with asgi_client(app) as c:
        done = await _issue(c, org_id, "done@x.co")
        await _issue(c, org_id, "twice@x.co")
        await _issue(c, org_id, "twice@x.co")  # replaces the first one
        await c.post("/api/invitations/accept", json={"token": _token_from(done), "password": PASSWORD})
        page = (await c.get("/api/invitations", headers=h)).json()
    assert [i["email"] for i in page["items"]] == ["twice@x.co"] and page["total"] == 1


async def test_listing_and_revoking_need_the_users_create_permission():
    app, sm = await _setup()
    uid, org_id, _ = await seed_user(sm, email="member@acme.com", username="member")
    _, member_id = await _seed_roles(sm, org_id)
    async with sm() as s:
        s.add(UserRole(user_id=uid, role_id=member_id))
        await s.commit()
    h = _jwt(uid, org_id, "member@acme.com")
    async with asgi_client(app) as c:
        assert (await c.get("/api/invitations", headers=h)).status_code == 403
        assert (await c.delete(f"/api/invitations/{uuid.uuid4()}", headers=h)).status_code == 403
        assert (await c.get("/api/invitations")).status_code == 401
        assert (await c.delete(f"/api/invitations/{uuid.uuid4()}")).status_code == 401


async def test_revoking_kills_the_link_and_is_audited():
    app, sm = await _setup()
    uid, org_id, _ = await seed_user(sm, email="admin@acme.com", username="admin")
    await _seed_roles(sm, org_id, grant_users_create_to=uid)
    h = _jwt(uid, org_id)
    async with asgi_client(app) as c:
        inv = await _issue(c, org_id, "gone@x.co")
        token = _token_from(inv)
        assert (await c.delete(f"/api/invitations/{inv['id']}", headers=h)).status_code == 204
        assert (await c.post("/api/invitations/inspect", json={"token": token})).status_code == 410
        assert (await c.post("/api/invitations/accept", json={"token": token, "password": PASSWORD})).status_code == 410
        assert (await c.get("/api/invitations", headers=h)).json()["total"] == 0
        # revoking twice is a conflict, not a silent success
        again = await c.delete(f"/api/invitations/{inv['id']}", headers=h)
        logs = (await c.get("/api/audit-logs", headers=h)).json()["items"]
    assert again.status_code == 409
    revoked = next(i for i in logs if i["action"] == "invitation.revoked")
    assert revoked["actor_email"] == "admin@acme.com" and revoked["resource_name"] == "gone@x.co"


async def test_cannot_revoke_another_tenants_or_an_accepted_invitation(fake_redis):
    app, sm = await _setup()
    uid, org_a, _ = await seed_user(sm, email="admin@acme.com", username="admin")
    await _seed_roles(sm, org_a, grant_users_create_to=uid)
    org_b = await seed_org(sm, name="Other")
    await _seed_roles(sm, org_b)
    h = _jwt(uid, org_a)
    async with asgi_client(app) as c:
        theirs = await _issue(c, org_b, "theirs@b.co")
        mine = await _issue(c, org_a, "mine@a.co")
        await c.post("/api/invitations/accept", json={"token": _token_from(mine), "password": PASSWORD})
        cross = await c.delete(f"/api/invitations/{theirs['id']}", headers=h)
        used = await c.delete(f"/api/invitations/{mine['id']}", headers=h)
        still = await c.post("/api/invitations/inspect", json={"token": _token_from(theirs)})
    assert cross.status_code == 404  # looks exactly like "no such invitation"
    assert still.status_code == 200  # and the other tenant's link is untouched
    assert used.status_code == 409


async def test_internal_surface_lists_across_orgs_and_revokes_any():
    app, sm = await _setup()
    org_a = await seed_org(sm, name="A")
    org_b = await seed_org(sm, name="B")
    await _seed_roles(sm, org_a)
    await _seed_roles(sm, org_b)
    async with asgi_client(app) as c:
        await _issue(c, org_a, "a@x.co")
        inv_b = await _issue(c, org_b, "b@x.co")
        everyone = (await c.get("/internal/v1/invitations", headers=auth_headers())).json()
        only_b = (await c.get("/internal/v1/invitations", params={"organization_id": str(org_b)}, headers=auth_headers())).json()
        revoked = await c.delete(
            f"/internal/v1/invitations/{inv_b['id']}",
            headers={**auth_headers(), "X-Actor-Email": "root@platform.local"},
        )
        after = (await c.get("/internal/v1/invitations", headers=auth_headers())).json()
        no_key = await c.get("/internal/v1/invitations")
        missing = await c.delete(f"/internal/v1/invitations/{uuid.uuid4()}", headers=auth_headers())
    assert everyone["total"] == 2 and {i["organization_id"] for i in everyone["items"]} == {str(org_a), str(org_b)}
    assert [i["email"] for i in only_b["items"]] == ["b@x.co"]
    assert revoked.status_code == 204 and [i["email"] for i in after["items"]] == ["a@x.co"]
    assert no_key.status_code == 401 and missing.status_code == 404
