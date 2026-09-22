"""Organizations CRUD, unknown-plan 422 (app-level), status/malformed validation. Also covers
the best-effort billing subscription notification on plan change (ADR-032/
add-billing-plan-subscription-linkage)."""

from __future__ import annotations

import json
import uuid

from conftest import auth_headers, make_billing_client, make_client, make_org_client, service_headers

H = auth_headers()


def test_create_with_valid_plan(client, plan_id):
    r = client.post("/v1/organizations", json={"name": "Acme Inc", "plan_id": plan_id}, headers=H)
    assert r.status_code == 201
    org = r.json()
    assert org["slug"] == "acme-inc" and org["status"] == "active" and org["plan_id"] == plan_id


def test_unknown_plan_422(client):
    r = client.post(
        "/v1/organizations", json={"name": "Ghost", "plan_id": str(uuid.uuid4())}, headers=H
    )
    assert r.status_code == 422 and r.json()["error"]["type"] == "unprocessable"


def test_patch_unknown_plan_422(client, plan_id):
    org = client.post("/v1/organizations", json={"name": "Beta", "plan_id": plan_id}, headers=H).json()
    r = client.patch(
        f"/v1/organizations/{org['id']}", json={"plan_id": str(uuid.uuid4())}, headers=H
    )
    assert r.status_code == 422 and r.json()["error"]["type"] == "unprocessable"
    # unchanged
    assert client.get(f"/v1/organizations/{org['id']}", headers=H).json()["plan_id"] == plan_id


def test_get_list_patch(client, plan_id):
    # need a second plan to change to
    second = client.post("/v1/plans", json={"name": "Second"}, headers=H).json()
    org = client.post("/v1/organizations", json={"name": "Orig", "plan_id": plan_id}, headers=H).json()

    assert client.get(f"/v1/organizations/{org['id']}", headers=H).status_code == 200
    assert any(o["id"] == org["id"] for o in client.get("/v1/organizations", headers=H).json())

    r = client.patch(
        f"/v1/organizations/{org['id']}",
        json={"name": "Renamed", "plan_id": second["id"], "status": "suspended"},
        headers=H,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Renamed" and body["slug"] == "renamed"
    assert body["plan_id"] == second["id"] and body["status"] == "suspended"


def test_duplicate_name_conflicts(client, plan_id):
    client.post("/v1/organizations", json={"name": "Twin", "plan_id": plan_id}, headers=H)
    r = client.post("/v1/organizations", json={"name": "Twin", "plan_id": plan_id}, headers=H)
    assert r.status_code == 409 and r.json()["error"]["type"] == "conflict"
    # Field-level detail lets a form point the error at the right input (§74).
    assert r.json()["error"]["field"] == "name"


def test_create_shows_zero_user_count_and_not_retryable():
    c, _ = make_client(org_client=make_org_client(user_counts={}))
    with c:
        pid = next(p["id"] for p in c.get("/v1/plans", headers=H).json() if p["name"] == "Free")
        org = c.post("/v1/organizations", json={"name": "Counted", "plan_id": pid}, headers=H).json()
        assert org["user_count"] == 0 and org["retryable"] is False


def test_organizations_list_reflects_bulk_user_counts():
    org_client = make_org_client()
    c, engine = make_client(org_client=org_client)
    with c:
        pid = next(p["id"] for p in c.get("/v1/plans", headers=H).json() if p["name"] == "Free")
        org = c.post("/v1/organizations", json={"name": "HasUsers", "plan_id": pid}, headers=H).json()

    # Reopen the same DB with an org_client that now reports a count for this org's id.
    c2, _ = make_client(engine=engine, org_client=make_org_client(user_counts={org["id"]: 3}))
    with c2:
        listed = next(o for o in c2.get("/v1/organizations", headers=H).json() if o["id"] == org["id"])
        assert listed["user_count"] == 3
        detail = c2.get(f"/v1/organizations/{org['id']}", headers=H).json()
        assert detail["user_count"] == 3


def test_list_search_filters_by_name(client, plan_id):
    client.post("/v1/organizations", json={"name": "Acme Corp", "plan_id": plan_id}, headers=H)
    client.post("/v1/organizations", json={"name": "Globex Inc", "plan_id": plan_id}, headers=H)

    r = client.get("/v1/organizations", params={"search": "acme"}, headers=H)
    names = [o["name"] for o in r.json()]
    assert names == ["Acme Corp"]


def test_internal_get_plan_for_org(client, plan_id):
    org = client.post("/v1/organizations", json={"name": "PlanLookup", "plan_id": plan_id}, headers=H).json()
    r = client.get(f"/internal/v1/organizations/{org['id']}/plan", headers=service_headers())
    assert r.status_code == 200
    plan = r.json()
    assert plan["id"] == plan_id and plan["name"] == "Free"


def test_internal_get_plan_absent_org_404(client):
    r = client.get(f"/internal/v1/organizations/{uuid.uuid4()}/plan", headers=service_headers())
    assert r.status_code == 404


def test_internal_get_plan_rejects_admin_key(client, plan_id):
    """The internal surface is gated by require_service_key, a bearer DISTINCT from the
    super-admin `admin_api_key` — the super-admin credential must not open this route."""
    org = client.post("/v1/organizations", json={"name": "Isolated", "plan_id": plan_id}, headers=H).json()
    r = client.get(f"/internal/v1/organizations/{org['id']}/plan", headers=H)
    assert r.status_code == 401


def test_v1_organizations_rejects_service_key(client, plan_id):
    """Symmetric check: the public /v1 surface is gated by require_admin_key — the service bearer
    must not open it."""
    r = client.post(
        "/v1/organizations", json={"name": "NoService", "plan_id": plan_id}, headers=service_headers()
    )
    assert r.status_code == 401


def test_user_count_fetch_failure_is_502():
    import httpx

    c, engine = make_client()
    with c:
        pid = next(p["id"] for p in c.get("/v1/plans", headers=H).json() if p["name"] == "Free")
        c.post("/v1/organizations", json={"name": "WillFail", "plan_id": pid}, headers=H)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/v1/organizations/user-counts":
            raise httpx.ConnectError("org cp down", request=request)
        return httpx.Response(201, json={"id": "stub", "name": "stub"})

    broken_counts_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://org-cp")
    c2, _ = make_client(engine=engine, org_client=broken_counts_client)
    with c2:
        r = c2.get("/v1/organizations", headers=H)
        assert r.status_code == 502 and r.json()["error"]["type"] == "upstream_error"


def test_delete_no_content(client, plan_id):
    org = client.post("/v1/organizations", json={"name": "Gone", "plan_id": plan_id}, headers=H).json()
    assert client.delete(f"/v1/organizations/{org['id']}", headers=H).status_code == 204
    assert client.get(f"/v1/organizations/{org['id']}", headers=H).status_code == 404


def test_delete_absent_404(client):
    r = client.delete(f"/v1/organizations/{uuid.uuid4()}", headers=H)
    assert r.status_code == 404 and r.json()["error"]["type"] == "not_found"


def test_patch_absent_404(client):
    r = client.patch(f"/v1/organizations/{uuid.uuid4()}", json={"name": "Ghost"}, headers=H)
    assert r.status_code == 404 and r.json()["error"]["type"] == "not_found"


def test_invalid_status_422(client, plan_id):
    org = client.post("/v1/organizations", json={"name": "Statusy", "plan_id": plan_id}, headers=H).json()
    r = client.patch(f"/v1/organizations/{org['id']}", json={"status": "frozen"}, headers=H)
    assert r.status_code == 422
    assert client.get(f"/v1/organizations/{org['id']}", headers=H).json()["status"] == "active"


def test_malformed_body_422(client):
    r = client.post("/v1/organizations", json={"name": "NoPlan"}, headers=H)  # missing plan_id
    assert r.status_code == 422


def test_plan_change_notifies_billing():
    recorder: list = []
    c, _ = make_client(billing_client=make_billing_client(recorder=recorder))
    with c:
        plan_a = c.post(
            "/v1/plans", json={"name": "PlanA", "stripe_price_id": "price_a"}, headers=H
        ).json()
        plan_b = c.post(
            "/v1/plans", json={"name": "PlanB", "stripe_price_id": "price_b"}, headers=H
        ).json()
        org = c.post(
            "/v1/organizations", json={"name": "Switcher", "plan_id": plan_a["id"]}, headers=H
        ).json()
        recorder.clear()  # ignore the creation-time notification; only the PATCH matters here

        r = c.patch(f"/v1/organizations/{org['id']}", json={"plan_id": plan_b["id"]}, headers=H)
        assert r.status_code == 200
        assert len(recorder) == 1
        sent = json.loads(recorder[0].content)
        assert sent["org_id"] == org["id"] and sent["stripe_price_id"] == "price_b"


def test_billing_outage_does_not_fail_plan_change():
    c, _ = make_client(billing_client=make_billing_client(unavailable=True))
    with c:
        plan_a = c.post(
            "/v1/plans", json={"name": "PlanA2", "stripe_price_id": "price_a"}, headers=H
        ).json()
        plan_b = c.post(
            "/v1/plans", json={"name": "PlanB2", "stripe_price_id": "price_b"}, headers=H
        ).json()
        org = c.post(
            "/v1/organizations", json={"name": "Resilient Switcher", "plan_id": plan_a["id"]},
            headers=H,
        ).json()

        r = c.patch(f"/v1/organizations/{org['id']}", json={"plan_id": plan_b["id"]}, headers=H)
        assert r.status_code == 200 and r.json()["plan_id"] == plan_b["id"]


def test_rename_without_plan_change_does_not_notify_billing():
    recorder: list = []
    c, _ = make_client(billing_client=make_billing_client(recorder=recorder))
    with c:
        plan = c.post(
            "/v1/plans", json={"name": "SamePlan", "stripe_price_id": "price_x"}, headers=H
        ).json()
        org = c.post(
            "/v1/organizations", json={"name": "Stays Put", "plan_id": plan["id"]}, headers=H
        ).json()
        recorder.clear()

        r = c.patch(f"/v1/organizations/{org['id']}", json={"name": "New Name"}, headers=H)
        assert r.status_code == 200
        assert recorder == []
