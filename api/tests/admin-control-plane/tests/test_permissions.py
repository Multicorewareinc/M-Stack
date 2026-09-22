"""Master permission list — CRUD, slug/no-org invariant, auth, internal read API, and seed.

Offline suite: injected aiosqlite, lifespan seeds the default catalog (SEED_PERMISSIONS on by
default in the test Settings). Postgres-specific UNIQUE(slug) behaviour is covered by the
integration run (integration/test_permissions_pg.py)."""

from __future__ import annotations

import uuid

from conftest import auth_headers, make_client, make_org_client, service_headers

H = auth_headers()

DEFAULT_CATALOG_SIZE = 11  # users.{r,c,u,d}, roles.{r,c,u,d}, organizations.{r,u}, apikey.manage


def test_create_permission_has_slug_no_org(client):
    r = client.post("/v1/permissions", json={"resource": "billing", "action": "read"}, headers=H)
    assert r.status_code == 201
    perm = r.json()
    assert perm["slug"] == "billing.read"
    assert perm["is_active"] is True
    assert uuid.UUID(perm["id"])  # server-generated id
    assert "organization_id" not in perm  # global catalog — no org scope


def test_duplicate_slug_conflicts(client):
    client.post("/v1/permissions", json={"resource": "billing", "action": "read"}, headers=H)
    r = client.post("/v1/permissions", json={"resource": "billing", "action": "read"}, headers=H)
    assert r.status_code == 409 and r.json()["error"]["type"] == "conflict"
    assert r.json()["error"]["field"] == "action"


def test_create_get_patch_delete(client):
    created = client.post(
        "/v1/permissions", json={"resource": "billing", "action": "create"}, headers=H
    )
    assert created.status_code == 201
    perm = created.json()

    got = client.get(f"/v1/permissions/{perm['id']}", headers=H)
    assert got.status_code == 200 and got.json()["id"] == perm["id"]

    patched = client.patch(
        f"/v1/permissions/{perm['id']}", json={"description": "Create invoices"}, headers=H
    )
    assert patched.status_code == 200 and patched.json()["description"] == "Create invoices"

    assert client.delete(f"/v1/permissions/{perm['id']}", headers=H).status_code == 204
    assert client.get(f"/v1/permissions/{perm['id']}", headers=H).status_code == 404


def test_permissions_require_admin_key(client):
    assert client.get("/v1/permissions").status_code == 401
    assert client.post("/v1/permissions", json={"resource": "x", "action": "y"}).status_code == 401
    bad = client.get("/v1/permissions", headers=auth_headers("wrong"))
    assert bad.status_code == 401 and bad.json()["error"]["type"] == "unauthorized"


def test_deactivate_blocked_when_referenced_by_org_role():
    """The Admin portal soft-deactivates (PATCH is_active:false), never hard-deletes (see the
    admin-permissions spec) — this is the path that actually needs the reverse-reference guard."""
    c, _ = make_client(org_client=make_org_client(permission_in_use=True))
    with c:
        perm = c.post("/v1/permissions", json={"resource": "billing", "action": "read"}, headers=H).json()
        r = c.patch(f"/v1/permissions/{perm['id']}", json={"is_active": False}, headers=H)
        assert r.status_code == 409 and r.json()["error"]["type"] == "conflict"
        assert c.get(f"/v1/permissions/{perm['id']}", headers=H).json()["is_active"] is True


def test_deactivate_allowed_when_unreferenced(client):
    perm = client.post("/v1/permissions", json={"resource": "billing", "action": "read"}, headers=H).json()
    r = client.patch(f"/v1/permissions/{perm['id']}", json={"is_active": False}, headers=H)
    assert r.status_code == 200 and r.json()["is_active"] is False


def test_reactivate_does_not_trigger_usage_check():
    """Flipping is_active back to True is never a deactivation — must not call the usage check
    (and must not be blocked by it) regardless of whether the permission is referenced."""
    c, _ = make_client(org_client=make_org_client(permission_in_use=True))
    with c:
        perm = c.post("/v1/permissions", json={"resource": "billing", "action": "read"}, headers=H).json()
        c.patch(f"/v1/permissions/{perm['id']}", json={"description": "x"}, headers=H)  # no-op is_active
        r = c.patch(f"/v1/permissions/{perm['id']}", json={"is_active": True}, headers=H)
        assert r.status_code == 200 and r.json()["is_active"] is True


def test_delete_blocked_when_referenced_by_org_role():
    """Council review fix: deleting a permission that's composed into any org's role must be
    blocked (409), not silently orphan the Org-side role_permissions row."""
    c, _ = make_client(org_client=make_org_client(permission_in_use=True))
    with c:
        perm = c.post("/v1/permissions", json={"resource": "billing", "action": "read"}, headers=H).json()
        r = c.delete(f"/v1/permissions/{perm['id']}", headers=H)
        assert r.status_code == 409 and r.json()["error"]["type"] == "conflict"
        # still there
        assert c.get(f"/v1/permissions/{perm['id']}", headers=H).status_code == 200


def test_delete_fails_closed_when_usage_check_unavailable():
    """Fail CLOSED (502), not silently allowed, when Org CP can't be asked whether the
    permission is still referenced."""
    c, _ = make_client(org_client=make_org_client(permission_in_use="unavailable"))
    with c:
        perm = c.post("/v1/permissions", json={"resource": "billing", "action": "read"}, headers=H).json()
        r = c.delete(f"/v1/permissions/{perm['id']}", headers=H)
        assert r.status_code == 502
        assert c.get(f"/v1/permissions/{perm['id']}", headers=H).status_code == 200  # untouched


def test_get_absent_404(client):
    r = client.get(f"/v1/permissions/{uuid.uuid4()}", headers=H)
    assert r.status_code == 404 and r.json()["error"]["type"] == "not_found"


def test_patch_absent_404(client):
    r = client.patch(f"/v1/permissions/{uuid.uuid4()}", json={"description": "x"}, headers=H)
    assert r.status_code == 404 and r.json()["error"]["type"] == "not_found"


def test_delete_absent_404(client):
    r = client.delete(f"/v1/permissions/{uuid.uuid4()}", headers=H)
    assert r.status_code == 404 and r.json()["error"]["type"] == "not_found"


def test_malformed_body_422(client):
    assert client.post("/v1/permissions", json={"resource": "onlyresource"}, headers=H).status_code == 422
    assert client.post("/v1/permissions", json={}, headers=H).status_code == 422


def test_internal_get_known_id_ok(client):
    perms = client.get("/internal/v1/permissions", headers=service_headers()).json()
    assert len(perms) >= DEFAULT_CATALOG_SIZE
    known = perms[0]["id"]
    r = client.get(f"/internal/v1/permissions/{known}", headers=service_headers())
    assert r.status_code == 200 and r.json()["id"] == known


def test_internal_get_unknown_id_404(client):
    r = client.get(f"/internal/v1/permissions/{uuid.uuid4()}", headers=service_headers())
    assert r.status_code == 404  # the signal Org CP uses to reject an invalid composition


def test_internal_permissions_rejects_admin_key(client):
    """require_service_key gates this route, a bearer DISTINCT from require_admin_key — the
    super-admin credential must not open it (council review fix: no shared-secret collapse)."""
    r = client.get("/internal/v1/permissions", headers=H)
    assert r.status_code == 401


def test_v1_permissions_rejects_service_key(client):
    """Symmetric check: the public /v1 surface must reject the service bearer."""
    r = client.get("/v1/permissions", headers=service_headers())
    assert r.status_code == 401


def test_seed_creates_default_catalog(client):
    perms = client.get("/v1/permissions", headers=H).json()
    slugs = {p["slug"] for p in perms}
    # Every slug the default org roles (org_admin/org_user, ADR-029/AD-04) bind MUST be present, so
    # org-init role seeding never fails closed on a missing slug (SP-01 catalog-completeness).
    required = {
        "users.read", "users.create", "users.update", "users.delete",
        "roles.read", "roles.create", "roles.update", "roles.delete",
        "organizations.read", "organizations.update", "apikey.manage",
    }
    assert required <= slugs
    assert len([p for p in perms if p["slug"] in slugs]) == len(slugs)  # no dup slugs seeded
    # exactly the default catalog seeded on a fresh DB
    assert len(perms) == DEFAULT_CATALOG_SIZE


def test_seed_idempotent_preserves_edits(client):
    # client fixture already booted once + seeded. Edit one permission, then reboot on same engine.
    perms = client.get("/v1/permissions", headers=H).json()
    target = next(p for p in perms if p["slug"] == "users.read")
    client.patch(f"/v1/permissions/{target['id']}", json={"description": "edited"}, headers=H)

    engine = client.app.state.engine
    c2, _ = make_client(engine=engine)
    with c2:
        perms2 = c2.get("/v1/permissions", headers=H).json()
        assert len(perms2) == DEFAULT_CATALOG_SIZE  # no duplicates on second boot
        edited = next(p for p in perms2 if p["slug"] == "users.read")
        assert edited["description"] == "edited"  # edit preserved
