"""RBAC resolution — effective permissions (union across roles), cross-org 404, the composed
role view, and the can() helper."""

from __future__ import annotations

import uuid

from conftest import auth_headers, make_admin_client, make_client, make_engine, org_headers

H = auth_headers()


def _init_org(c, name="Org"):
    oid = str(uuid.uuid4())
    assert c.post("/internal/v1/organizations", json={"id": oid, "name": name}, headers=H).status_code == 201
    return oid


def _user(c, org, username="john"):
    r = c.post("/v1/users", json={"username": username, "email": f"{username}@e.com"}, headers=org_headers(org))
    return r.json()["id"]


def _role(c, org, name, permission_ids):
    rid = c.post("/v1/roles", json={"name": name}, headers=org_headers(org)).json()["id"]
    r = c.put(
        f"/v1/roles/{rid}/permissions",
        json={"permission_ids": [str(p) for p in permission_ids]},
        headers=org_headers(org),
    )
    assert r.status_code == 200, r.text
    return rid


def test_effective_permissions_union():
    p1, p2, p3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    c, _ = make_client(admin_client=make_admin_client([p1, p2, p3]))
    with c:
        org = _init_org(c)
        uid = _user(c, org)
        r1 = _role(c, org, "R1", [p1, p2])
        r2 = _role(c, org, "R2", [p2, p3])
        c.put(f"/v1/users/{uid}/roles", json={"role_ids": [r1, r2]}, headers=org_headers(org))
        got = c.get(f"/v1/users/{uid}/permissions", headers=org_headers(org)).json()
        assert set(got["permission_ids"]) == {str(p1), str(p2), str(p3)}
        assert len(got["permission_ids"]) == 3  # deduplicated


def test_effective_permissions_empty():
    c, _ = make_client()
    with c:
        org = _init_org(c)
        uid = _user(c, org)  # no roles
        got = c.get(f"/v1/users/{uid}/permissions", headers=org_headers(org)).json()
        assert got["permission_ids"] == []


def test_effective_permissions_cross_org_404():
    c, _ = make_client()
    with c:
        org_a, org_b = _init_org(c, "A"), _init_org(c, "B")
        uid_b = _user(c, org_b, "bob")
        r = c.get(f"/v1/users/{uid_b}/permissions", headers=org_headers(org_a))
        assert r.status_code == 404


def test_composed_role_view():
    p1, p2 = uuid.uuid4(), uuid.uuid4()
    c, _ = make_client(admin_client=make_admin_client([p1, p2]))
    with c:
        org = _init_org(c)
        uid = _user(c, org)
        rid = _role(c, org, "Admin", [p1, p2])
        c.put(f"/v1/users/{uid}/roles", json={"role_ids": [rid]}, headers=org_headers(org))
        view = c.get(f"/internal/v1/organizations/{org}/users/{uid}/roles", headers=H).json()
        assert len(view) == 1
        assert view[0]["name"] == "Admin"
        assert set(view[0]["permission_ids"]) == {str(p1), str(p2)}


def test_composed_view_cross_org_404():
    c, _ = make_client()
    with c:
        org_a, org_b = _init_org(c, "A"), _init_org(c, "B")
        uid_b = _user(c, org_b, "bob")
        r = c.get(f"/internal/v1/organizations/{org_a}/users/{uid_b}/roles", headers=H)
        assert r.status_code == 404


async def test_can_reflects_effective_set():
    """Directly exercises can() over a standalone engine (no TestClient — avoids cross-loop issues)."""
    from db import Base
    from modules.rbac.service import can
    from modules.roles.models import Role, RolePermission, UserRole
    from modules.users.models import User

    engine = make_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    from sqlalchemy.ext.asyncio import async_sessionmaker

    org, uid, rid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    p1, p9 = uuid.uuid4(), uuid.uuid4()
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        s.add(User(id=uid, organization_id=org, username="u", email="u@e.com"))
        s.add(Role(id=rid, organization_id=org, name="R"))
        s.add(RolePermission(role_id=rid, permission_id=p1))
        s.add(UserRole(user_id=uid, role_id=rid))
        await s.commit()
    async with sm() as s:
        assert await can(s, org, uid, p1) is True
        assert await can(s, org, uid, p9) is False
    await engine.dispose()


async def test_effective_permissions_scoped_on_role_org_defense_in_depth():
    """Council review defense-in-depth fix: effective_permission_ids must join on
    Role.organization_id, not just user_id — otherwise a cross-org RolePermission (unreachable via
    normal writes, but possible via a bad migration or a future direct write) would leak into the
    effective set. This directly constructs that impossible-via-the-API state to prove the join
    excludes it."""
    from db import Base
    from modules.rbac.service import effective_permission_ids
    from modules.roles.models import Role, RolePermission, UserRole
    from modules.users.models import User

    engine = make_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    from sqlalchemy.ext.asyncio import async_sessionmaker

    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    uid = uuid.uuid4()
    role_a, role_b = uuid.uuid4(), uuid.uuid4()
    perm_a, perm_b = uuid.uuid4(), uuid.uuid4()
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        s.add(User(id=uid, organization_id=org_a, username="u", email="u@e.com"))
        s.add(Role(id=role_a, organization_id=org_a, name="InOrg"))
        s.add(Role(id=role_b, organization_id=org_b, name="OtherOrg"))  # a DIFFERENT org's role
        s.add(RolePermission(role_id=role_a, permission_id=perm_a))
        s.add(RolePermission(role_id=role_b, permission_id=perm_b))
        s.add(UserRole(user_id=uid, role_id=role_a))
        # This UserRole row could never be created via the API (set_user_roles validates every
        # role_id is in-org first) — inserted directly to simulate the bad-migration/future-bug case.
        s.add(UserRole(user_id=uid, role_id=role_b))
        await s.commit()
    async with sm() as s:
        ids = await effective_permission_ids(s, org_a, uid)
    assert ids == [perm_a]  # perm_b must NOT leak in despite the cross-org UserRole row
    await engine.dispose()
