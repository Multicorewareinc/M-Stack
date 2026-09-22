"""Roles — tenant-scoped CRUD, permission composition validated against Admin CP (stubbed via
MockTransport), and user-role assignment."""

from __future__ import annotations

import uuid

from conftest import auth_headers, make_admin_client, make_client, make_engine, org_headers

H = auth_headers()


def _init_org(client, name="Org"):
    oid = str(uuid.uuid4())
    assert client.post(
        "/internal/v1/organizations", json={"id": oid, "name": name}, headers=H
    ).status_code == 201
    return oid


def _make_user(client, org_id, username="john"):
    r = client.post(
        "/v1/users",
        json={"username": username, "email": f"{username}@e.com"},
        headers=org_headers(org_id),
    )
    assert r.status_code == 201
    return r.json()["id"]


def _make_role(client, org_id, name="Admin"):
    r = client.post("/v1/roles", json={"name": name}, headers=org_headers(org_id))
    assert r.status_code == 201
    return r.json()["id"]


def test_create_role_unique_per_org(client):
    org_a, org_b = _init_org(client, "A"), _init_org(client, "B")
    assert client.post("/v1/roles", json={"name": "Admin"}, headers=org_headers(org_a)).status_code == 201
    dup = client.post("/v1/roles", json={"name": "Admin"}, headers=org_headers(org_a))
    assert dup.status_code == 409
    assert dup.json()["error"]["field"] == "name"
    # same name in a different org is allowed
    assert client.post("/v1/roles", json={"name": "Admin"}, headers=org_headers(org_b)).status_code == 201


def test_cross_org_role_404(client):
    org_a, org_b = _init_org(client, "A"), _init_org(client, "B")
    rid = _make_role(client, org_b, "Admin")
    assert client.get(f"/v1/roles/{rid}", headers=org_headers(org_a)).status_code == 404
    assert client.patch(f"/v1/roles/{rid}", json={"description": "x"}, headers=org_headers(org_a)).status_code == 404
    assert client.delete(f"/v1/roles/{rid}", headers=org_headers(org_a)).status_code == 404


def test_delete_system_role_blocked(client):
    """The default org_admin/org_user roles (seeded at org-init, ADR-029) are marked
    is_system_role=True and must not be deletable — previously this flag was stored/returned but
    never enforced."""
    org = _init_org(client)
    roles = {r["name"]: r for r in client.get("/v1/roles", headers=org_headers(org)).json()}
    admin_role = roles["org_admin"]
    assert admin_role["is_system_role"] is True
    r = client.delete(f"/v1/roles/{admin_role['id']}", headers=org_headers(org))
    assert r.status_code == 409 and r.json()["error"]["type"] == "conflict"
    # still there
    assert client.get(f"/v1/roles/{admin_role['id']}", headers=org_headers(org)).status_code == 200


def test_delete_non_system_role_still_allowed(client):
    org = _init_org(client)
    rid = _make_role(client, org, "Custom")
    assert client.delete(f"/v1/roles/{rid}", headers=org_headers(org)).status_code == 204


def test_same_org_unknown_role_404(client):
    """A role id that simply doesn't exist (not a cross-org case) must also 404, same-org."""
    org = _init_org(client)
    fake_id = str(uuid.uuid4())
    assert client.get(f"/v1/roles/{fake_id}", headers=org_headers(org)).status_code == 404
    assert client.patch(f"/v1/roles/{fake_id}", json={"description": "x"}, headers=org_headers(org)).status_code == 404
    assert client.delete(f"/v1/roles/{fake_id}", headers=org_headers(org)).status_code == 404


def test_compose_cross_org_role_404():
    """A caller in org A must not be able to set org B's role's permission composition (council
    review: no isolation test existed at all for this endpoint)."""
    p1 = uuid.uuid4()
    c, _ = make_client(admin_client=make_admin_client([p1]))
    with c:
        org_a, org_b = _init_org(c, "A"), _init_org(c, "B")
        rid_b = _make_role(c, org_b, "Admin")
        r = c.put(
            f"/v1/roles/{rid_b}/permissions",
            json={"permission_ids": [str(p1)]},
            headers=org_headers(org_a),
        )
        assert r.status_code == 404
        # nothing composed on org B's role
        got = c.get(f"/v1/roles/{rid_b}/permissions", headers=org_headers(org_b)).json()
        assert got["permission_ids"] == []


def test_compose_valid_permissions():
    p1, p2 = uuid.uuid4(), uuid.uuid4()
    c, _ = make_client(admin_client=make_admin_client([p1, p2]))
    with c:
        org = _init_org(c)
        rid = _make_role(c, org)
        r = c.put(
            f"/v1/roles/{rid}/permissions",
            json={"permission_ids": [str(p1), str(p2)]},
            headers=org_headers(org),
        )
        assert r.status_code == 200
        got = c.get(f"/v1/roles/{rid}/permissions", headers=org_headers(org)).json()
        assert set(got["permission_ids"]) == {str(p1), str(p2)}


def test_compose_unknown_permission_422():
    p1, px = uuid.uuid4(), uuid.uuid4()
    c, _ = make_client(admin_client=make_admin_client([p1]))  # px is unknown
    with c:
        org = _init_org(c)
        rid = _make_role(c, org)
        r = c.put(
            f"/v1/roles/{rid}/permissions",
            json={"permission_ids": [str(p1), str(px)]},
            headers=org_headers(org),
        )
        assert r.status_code == 422
        # atomic: nothing persisted (not even the valid p1)
        got = c.get(f"/v1/roles/{rid}/permissions", headers=org_headers(org)).json()
        assert got["permission_ids"] == []


def test_compose_admin_unavailable_502():
    # Provision + create the role with an available admin stub (org-init seeds default roles), then
    # compose against an unavailable one (shared engine) to exercise the fail-closed 502 path.
    p1 = uuid.uuid4()
    engine = make_engine()
    c_ok, _ = make_client(engine=engine, admin_client=make_admin_client([p1]))
    c_bad, _ = make_client(engine=engine, admin_client=make_admin_client([p1], unavailable=True))
    with c_ok, c_bad:
        org = _init_org(c_ok)
        rid = _make_role(c_ok, org)
        r = c_bad.put(
            f"/v1/roles/{rid}/permissions",
            json={"permission_ids": [str(p1)]},
            headers=org_headers(org),
        )
        assert r.status_code == 502
        got = c_ok.get(f"/v1/roles/{rid}/permissions", headers=org_headers(org)).json()
        assert got["permission_ids"] == []


def test_assign_role_to_user(client):
    org = _init_org(client)
    uid = _make_user(client, org)
    rid = _make_role(client, org)
    r = client.put(f"/v1/users/{uid}/roles", json={"role_ids": [rid]}, headers=org_headers(org))
    assert r.status_code == 200
    got = client.get(f"/v1/users/{uid}/roles", headers=org_headers(org)).json()
    assert got["role_ids"] == [rid]


def test_assign_cross_org_role_404(client):
    org_a, org_b = _init_org(client, "A"), _init_org(client, "B")
    uid = _make_user(client, org_a, "u")
    rid_b = _make_role(client, org_b, "Admin")  # role in org B
    r = client.put(f"/v1/users/{uid}/roles", json={"role_ids": [rid_b]}, headers=org_headers(org_a))
    assert r.status_code == 404
    # nothing assigned
    assert client.get(f"/v1/users/{uid}/roles", headers=org_headers(org_a)).json()["role_ids"] == []


def test_internal_permission_usage_reflects_composition():
    """New endpoint (council review fix) backing Admin CP's reverse-reference check before it
    deletes a master-list permission."""
    p1, p2 = uuid.uuid4(), uuid.uuid4()
    c, _ = make_client(admin_client=make_admin_client([p1, p2]))
    with c:
        org = _init_org(c)
        rid = _make_role(c, org)
        assert c.get(
            "/internal/v1/roles/permission-usage", params={"permission_id": str(p1)}, headers=H
        ).json() == {"in_use": False}
        c.put(
            f"/v1/roles/{rid}/permissions",
            json={"permission_ids": [str(p1)]},
            headers=org_headers(org),
        )
        assert c.get(
            "/internal/v1/roles/permission-usage", params={"permission_id": str(p1)}, headers=H
        ).json() == {"in_use": True}
        # An unrelated permission id, never composed anywhere, stays not-in-use.
        assert c.get(
            "/internal/v1/roles/permission-usage", params={"permission_id": str(p2)}, headers=H
        ).json() == {"in_use": False}


def test_assign_cross_org_target_user_404(client):
    """The TARGET user, not just the role, must be org-scoped (council review: only the
    cross-org-role case was tested; a cross-org target user was not)."""
    org_a, org_b = _init_org(client, "A"), _init_org(client, "B")
    uid_b = _make_user(client, org_b, "victim")  # user lives in org B
    rid_a = _make_role(client, org_a, "Admin")  # role lives in org A
    r = client.put(f"/v1/users/{uid_b}/roles", json={"role_ids": [rid_a]}, headers=org_headers(org_a))
    assert r.status_code == 404
    # nothing assigned to org B's user
    assert client.get(f"/v1/users/{uid_b}/roles", headers=org_headers(org_b)).json()["role_ids"] == []
