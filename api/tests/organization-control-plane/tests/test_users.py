"""Users — tenant-scoped CRUD, cross-org protection, status, and internal read endpoints."""

from __future__ import annotations

import uuid

from conftest import auth_headers, org_headers

H = auth_headers()


def _init_org(client, name="Org"):
    oid = str(uuid.uuid4())
    r = client.post("/internal/v1/organizations", json={"id": oid, "name": name}, headers=H)
    assert r.status_code == 201
    return oid


def _make_user(client, org_id, username="john", email=None):
    email = email or f"{username}@example.com"
    return client.post(
        "/v1/users",
        json={"username": username, "email": email, "display_name": "John"},
        headers=org_headers(org_id),
    )


def test_create_and_get_user(client):
    org = _init_org(client)
    r = _make_user(client, org)
    assert r.status_code == 201
    user = r.json()
    assert user["organization_id"] == org and user["status"] == "active"
    assert user["metadata"] == {}  # serialization alias meta -> metadata

    got = client.get(f"/v1/users/{user['id']}", headers=org_headers(org))
    assert got.status_code == 200 and got.json()["id"] == user["id"]


def test_list_is_tenant_scoped(client):
    org_a, org_b = _init_org(client, "A"), _init_org(client, "B")
    _make_user(client, org_a, "a1")
    _make_user(client, org_b, "b1")
    a_users = client.get("/v1/users", headers=org_headers(org_a)).json()
    assert [u["username"] for u in a_users] == ["a1"]  # org B's user never appears


def test_email_unique_per_org(client):
    org_a, org_b = _init_org(client, "A"), _init_org(client, "B")
    assert _make_user(client, org_a, "u1", "x@acme.com").status_code == 201
    dup = _make_user(client, org_a, "u2", "x@acme.com")
    assert dup.status_code == 409 and dup.json()["error"]["type"] == "conflict"
    assert dup.json()["error"]["field"] == "email"
    # same email in a different org is allowed
    assert _make_user(client, org_b, "u3", "x@acme.com").status_code == 201


def test_username_unique_per_org(client):
    org = _init_org(client)
    assert _make_user(client, org, "dupname", "a@acme.com").status_code == 201
    dup = _make_user(client, org, "dupname", "b@acme.com")
    assert dup.status_code == 409 and dup.json()["error"]["type"] == "conflict"
    assert dup.json()["error"]["field"] == "username"


def test_create_unknown_org_422(client):
    r = _make_user(client, str(uuid.uuid4()))  # org never initialized
    assert r.status_code == 422


def test_missing_context_or_body_422(client):
    org = _init_org(client)
    # missing X-Organization-Id
    assert client.post("/v1/users", json={"username": "x", "email": "x@e.com"}, headers=H).status_code == 422
    # missing required body fields
    assert client.post("/v1/users", json={"username": "x"}, headers=org_headers(org)).status_code == 422


def test_cross_org_read_404(client):
    org_a, org_b = _init_org(client, "A"), _init_org(client, "B")
    u = _make_user(client, org_b, "bob").json()
    # org A tries to read org B's user
    assert client.get(f"/v1/users/{u['id']}", headers=org_headers(org_a)).status_code == 404


def test_internal_bulk_user_counts(client):
    org_a, org_b = _init_org(client, "A"), _init_org(client, "B")
    _make_user(client, org_a, "a1")
    _make_user(client, org_a, "a2")
    _make_user(client, org_b, "b1")

    r = client.get(
        "/internal/v1/organizations/user-counts", params=[("ids", org_a), ("ids", org_b)], headers=H
    )
    assert r.status_code == 200
    counts = r.json()["counts"]
    assert counts[org_a] == 2 and counts[org_b] == 1


def test_internal_bulk_user_counts_omits_orgs_with_no_users(client):
    org = _init_org(client)
    r = client.get("/internal/v1/organizations/user-counts", params=[("ids", org)], headers=H)
    assert r.json()["counts"] == {}


def test_internal_active_user_count_is_platform_wide(client):
    org_a, org_b = _init_org(client, "A"), _init_org(client, "B")
    _make_user(client, org_a, "a1")
    _make_user(client, org_b, "b1")
    r = client.get("/internal/v1/organizations/active-user-count", headers=H)
    assert r.status_code == 200 and r.json()["count"] == 2


def test_internal_platform_list_returns_every_org_user(client):
    org_a, org_b = _init_org(client, "A"), _init_org(client, "B")
    _make_user(client, org_a, "a1")
    _make_user(client, org_b, "b1")
    r = client.get("/internal/v1/users", headers=H)
    assert r.status_code == 200
    usernames = {u["username"] for u in r.json()}
    assert usernames == {"a1", "b1"}


def test_internal_platform_get_resolves_without_org_context(client):
    org = _init_org(client)
    created = _make_user(client, org, "solo").json()
    r = client.get(f"/internal/v1/users/{created['id']}", headers=H)
    assert r.status_code == 200
    assert r.json()["organization_id"] == org


def test_internal_platform_get_absent_404(client):
    r = client.get(f"/internal/v1/users/{uuid.uuid4()}", headers=H)
    assert r.status_code == 404


def test_cross_org_mutation_404(client):
    org_a, org_b = _init_org(client, "A"), _init_org(client, "B")
    u = _make_user(client, org_b, "bob").json()
    assert client.patch(
        f"/v1/users/{u['id']}", json={"status": "suspended"}, headers=org_headers(org_a)
    ).status_code == 404
    assert client.delete(f"/v1/users/{u['id']}", headers=org_headers(org_a)).status_code == 404
    # org B's user is untouched
    still = client.get(f"/v1/users/{u['id']}", headers=org_headers(org_b)).json()
    assert still["status"] == "active"


def test_status_transition(client):
    org = _init_org(client)
    u = _make_user(client, org).json()
    susp = client.patch(f"/v1/users/{u['id']}", json={"status": "suspended"}, headers=org_headers(org))
    assert susp.status_code == 200 and susp.json()["status"] == "suspended"
    # Re-read via a fresh GET, not just the mutation's own echo — confirms the status was actually
    # PERSISTED, not merely reflected back in the PATCH response body.
    got = client.get(f"/v1/users/{u['id']}", headers=org_headers(org))
    assert got.json()["status"] == "suspended"

    react = client.patch(f"/v1/users/{u['id']}", json={"status": "active"}, headers=org_headers(org))
    assert react.json()["status"] == "active"
    got2 = client.get(f"/v1/users/{u['id']}", headers=org_headers(org))
    assert got2.json()["status"] == "active"


def test_internal_list_users(client):
    org_a, org_b = _init_org(client, "A"), _init_org(client, "B")
    _make_user(client, org_a, "a1")
    _make_user(client, org_b, "b1")
    users = client.get(f"/internal/v1/organizations/{org_a}/users", headers=H).json()
    assert [u["username"] for u in users] == ["a1"]


def test_internal_user_cross_org_404(client):
    org_a, org_b = _init_org(client, "A"), _init_org(client, "B")
    u = _make_user(client, org_b, "bob").json()
    r = client.get(f"/internal/v1/organizations/{org_a}/users/{u['id']}", headers=H)
    assert r.status_code == 404
