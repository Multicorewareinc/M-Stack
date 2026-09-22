"""Permission-catalog proxy — read-only, tenant-scoped, forwards Admin CP's master list
verbatim (ADR-018/019)."""

from __future__ import annotations

import uuid

from conftest import (
    auth_headers,
    default_permission_catalog,
    make_admin_client,
    make_client,
    make_engine,
    org_headers,
)

H = auth_headers()


def _init_org(client):
    oid = str(uuid.uuid4())
    r = client.post("/internal/v1/organizations", json={"id": oid, "name": "Org"}, headers=H)
    assert r.status_code == 201
    return oid


def test_list_forwards_admin_catalog_verbatim():
    # The catalog must contain the default role slugs (org-init seeds default roles from it), so
    # start from the default catalog and append a distinctive extra entry to prove verbatim forward.
    catalog = default_permission_catalog() + [
        {"id": str(uuid.uuid4()), "resource": "billing", "action": "read", "slug": "billing.read"}
    ]
    c, _ = make_client(admin_client=make_admin_client(permission_catalog=catalog))
    with c:
        org = _init_org(c)
        r = c.get("/v1/permissions", headers=org_headers(org))
        assert r.status_code == 200
        assert r.json() == catalog


def test_list_upstream_failure_is_502():
    # Provision with an available admin stub (org-init seeds default roles), then read the catalog
    # proxy against an unavailable one (shared engine) to exercise the 502 path.
    engine = make_engine()
    c_ok, _ = make_client(engine=engine)
    c_bad, _ = make_client(engine=engine, admin_client=make_admin_client(unavailable=True))
    with c_ok, c_bad:
        org = _init_org(c_ok)
        r = c_bad.get("/v1/permissions", headers=org_headers(org))
        assert r.status_code == 502 and r.json()["error"]["type"] == "upstream_error"


def test_list_requires_service_key():
    c, _ = make_client()
    with c:
        org = _init_org(c)
        r = c.get("/v1/permissions", headers={"X-Organization-Id": org})  # no bearer
        assert r.status_code == 401


def test_list_works_without_org_header():
    """The master catalog is deliberately NOT tenant-scoped (router docstring: "not tenant data,
    so no require_org_context here") — confirms that design decision is intentional and covered,
    not an accidental gap. A caller with only the service bearer, no X-Organization-Id at all,
    must still succeed."""
    c, _ = make_client()
    with c:
        r = c.get("/v1/permissions", headers=H)  # service bearer only, no org header
        assert r.status_code == 200
        assert isinstance(r.json(), list) and len(r.json()) > 0
