"""Platform summary — counts + status breakdown, active-user count via Org CP (§184)."""

from __future__ import annotations

import httpx

from conftest import auth_headers, make_client

H = auth_headers()


def _make_org_client_with_active_users(count: int) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/v1/organizations/active-user-count":
            return httpx.Response(200, json={"count": count})
        return httpx.Response(200, json={"id": "stub", "name": "stub"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://org-cp")


def test_summary_counts_orgs_plans_and_status_breakdown():
    c, _ = make_client(org_client=_make_org_client_with_active_users(12))
    with c:
        pid = next(p["id"] for p in c.get("/v1/plans", headers=H).json() if p["name"] == "Free")
        c.post("/v1/organizations", json={"name": "One", "plan_id": pid}, headers=H)
        c.post("/v1/organizations", json={"name": "Two", "plan_id": pid}, headers=H)

        r = c.get("/v1/platform-summary", headers=H)
        assert r.status_code == 200
        body = r.json()
        assert body["organizations"] == 2
        assert body["active"] == 2
        assert body["provisioning"] == 0
        assert body["failed"] == 0
        assert body["plans"] == 3  # the three seeded default plans
        assert body["active_users"] == 12


def test_active_user_count_upstream_failure_is_502():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("org cp down", request=request)

    org_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://org-cp")
    c, _ = make_client(org_client=org_client)
    with c:
        r = c.get("/v1/platform-summary", headers=H)
        assert r.status_code == 502 and r.json()["error"]["type"] == "upstream_error"
