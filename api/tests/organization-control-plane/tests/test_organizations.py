"""Organization tenant record — idempotent internal init and read, plus the tenant-scoped
public summary endpoint (counts + plan entitlement, §55, §100)."""

from __future__ import annotations

import uuid

from conftest import auth_headers, make_admin_client, make_client, make_engine, org_headers

H = auth_headers()


def test_init_creates_tenant(client):
    oid = str(uuid.uuid4())
    r = client.post("/internal/v1/organizations", json={"id": oid, "name": "Acme"}, headers=H)
    assert r.status_code == 201
    org = r.json()
    assert org["id"] == oid and org["status"] == "active" and org["settings"] == {}

    got = client.get(f"/internal/v1/organizations/{oid}", headers=H)
    assert got.status_code == 200 and got.json()["id"] == oid


def test_init_idempotent(client):
    oid = str(uuid.uuid4())
    first = client.post("/internal/v1/organizations", json={"id": oid, "name": "Acme"}, headers=H)
    assert first.status_code == 201
    # Re-init with the same id: 200 (not 201), no error, no duplicate, name unchanged.
    second = client.post(
        "/internal/v1/organizations", json={"id": oid, "name": "Acme Renamed"}, headers=H
    )
    assert second.status_code == 200
    assert second.json()["id"] == oid and second.json()["name"] == "Acme"


def test_get_absent_404(client):
    r = client.get(f"/internal/v1/organizations/{uuid.uuid4()}", headers=H)
    assert r.status_code == 404 and r.json()["error"]["type"] == "not_found"


def test_init_requires_service_key(client):
    r = client.post("/internal/v1/organizations", json={"id": str(uuid.uuid4()), "name": "X"})
    assert r.status_code == 401


def test_summary_combines_counts_and_plan():
    plan = {
        "name": "Pro",
        "tpm": 100000,
        "rpm": 600,
        "quota_monthly_tokens": 50000000,
    }
    c, _ = make_client(admin_client=make_admin_client(plan=plan))
    with c:
        oid = str(uuid.uuid4())
        c.post("/internal/v1/organizations", json={"id": oid, "name": "Acme Corp"}, headers=H)
        c.post("/v1/users", json={"username": "u1", "email": "u1@e.com"}, headers=org_headers(oid))
        c.post("/v1/users", json={"username": "u2", "email": "u2@e.com"}, headers=org_headers(oid))
        c.post("/v1/roles", json={"name": "Admin"}, headers=org_headers(oid))

        r = c.get("/v1/organization/summary", headers=org_headers(oid))
        assert r.status_code == 200
        body = r.json()
        assert body["organization"] == {"name": "Acme Corp", "slug": "acme-corp", "status": "active"}
        # role_count is 3: the two default system roles seeded at org-init (org_admin, org_user,
        # ADR-029/AD-04) plus the "Admin" role created above.
        assert body["user_count"] == 2 and body["role_count"] == 3
        assert body["plan"]["name"] == "Pro" and body["plan"]["tpm"] == 100000


def test_summary_requires_org_context():
    c, _ = make_client()
    with c:
        oid = str(uuid.uuid4())
        c.post("/internal/v1/organizations", json={"id": oid, "name": "Acme"}, headers=H)
        r = c.get("/v1/organization/summary", headers=H)  # no X-Organization-Id
        assert r.status_code == 422


def test_summary_plan_upstream_failure_is_502():
    # Org-init now seeds default roles from the Admin catalog, so provision with an available admin
    # stub, then exercise the summary's plan lookup against an unavailable one (shared engine).
    engine = make_engine()
    c_ok, _ = make_client(engine=engine)
    c_bad, _ = make_client(engine=engine, admin_client=make_admin_client(unavailable=True))
    with c_ok, c_bad:
        oid = str(uuid.uuid4())
        assert c_ok.post(
            "/internal/v1/organizations", json={"id": oid, "name": "Acme"}, headers=H
        ).status_code == 201
        r = c_bad.get("/v1/organization/summary", headers=org_headers(oid))
        assert r.status_code == 502 and r.json()["error"]["type"] == "upstream_error"
