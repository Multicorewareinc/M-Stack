"""Default org-role seeding at org-init (ADR-029/AD-04, SP-01) — offline suite.

Org-init seeds two system roles (org_admin, org_user) bound to permission ids resolved from the
Admin CP master catalog. These tests pin the single-catalog-fetch and fail-closed invariants; the
Postgres-backed correctness/idempotency lives in integration/test_default_roles_pg.py.
"""

from __future__ import annotations

import uuid

import httpx
from conftest import (
    auth_headers,
    default_permission_catalog,
    make_client,
    make_engine,
    org_headers,
)

H = auth_headers()


def _counting_admin_client(catalog):
    """A stub Admin CP client that counts GET /internal/v1/permissions (list) calls."""
    counter = {"list_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/internal/v1/permissions":
            counter["list_calls"] += 1
            return httpx.Response(200, json=catalog)
        if path.endswith("/plan"):
            return httpx.Response(200, json={
                "id": str(uuid.uuid4()), "name": "Free", "tpm": 1, "rpm": 1,
                "quota_monthly_tokens": 1, "is_default": True,
                "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
            })
        pid = path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"id": pid})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://admin-cp")
    return client, counter


def test_seed_resolves_ids_in_single_catalog_fetch():
    admin_client, counter = _counting_admin_client(default_permission_catalog())
    c, _ = make_client(admin_client=admin_client)
    with c:
        counter["list_calls"] = 0  # ignore the lifespan-startup apikey.manage resolver fetch
        oid = str(uuid.uuid4())
        assert c.post(
            "/internal/v1/organizations", json={"id": oid, "name": "Acme"}, headers=H
        ).status_code == 201
        # Exactly one catalog fetch for the whole org-init seed (no per-permission round trip).
        assert counter["list_calls"] == 1
        # Both default roles are present.
        roles = c.get("/v1/roles", headers=org_headers(oid)).json()
        assert {r["name"] for r in roles} == {"org_admin", "org_user"}


def test_seed_fails_closed_when_catalog_unavailable():
    # Admin CP unreachable at org-init -> upstream error, and no org/roles persisted (atomic).
    engine = make_engine()

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("admin cp down", request=request)

    down_client = httpx.AsyncClient(transport=httpx.MockTransport(down), base_url="http://admin-cp")
    c_bad, _ = make_client(engine=engine, admin_client=down_client)
    with c_bad:
        oid = str(uuid.uuid4())
        r = c_bad.post("/internal/v1/organizations", json={"id": oid, "name": "Acme"}, headers=H)
        assert r.status_code == 502 and r.json()["error"]["type"] == "upstream_error"
        # Nothing persisted — the org row rolled back with the failed seed (no partial provisioning).
        assert c_bad.get(f"/internal/v1/organizations/{oid}", headers=H).status_code == 404


def test_seed_fails_closed_when_required_slug_missing():
    # Catalog reachable but missing a required slug -> fail closed, no partial roles.
    partial = [c for c in default_permission_catalog() if c["slug"] != "apikey.manage"]
    engine = make_engine()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/v1/permissions":
            return httpx.Response(200, json=partial)
        return httpx.Response(200, json={"id": request.url.path.rsplit("/", 1)[-1]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://admin-cp")
    c, _ = make_client(engine=engine, admin_client=client)
    with c:
        oid = str(uuid.uuid4())
        r = c.post("/internal/v1/organizations", json={"id": oid, "name": "Acme"}, headers=H)
        assert r.status_code == 502
        assert c.get(f"/internal/v1/organizations/{oid}", headers=H).status_code == 404
