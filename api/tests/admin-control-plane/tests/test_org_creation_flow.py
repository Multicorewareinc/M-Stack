"""Organization creation as a synchronous Admin CP → Org CP workflow (SP-07):
provisioning → active/failed, Org CP init stubbed via MockTransport. Also covers the
best-effort billing subscription notification (ADR-032/add-billing-plan-subscription-linkage)
on org creation and retry."""

from __future__ import annotations

import json
import uuid

from conftest import auth_headers, make_billing_client, make_client, make_org_client

H = auth_headers()


def _seed_plan_id(client):
    plans = client.get("/v1/plans", headers=H).json()
    return next(p["id"] for p in plans if p["name"] == "Free")


def _priced_plan_id(client, stripe_price_id="price_abc"):
    return client.post(
        "/v1/plans", json={"name": "Priced", "stripe_price_id": stripe_price_id}, headers=H
    ).json()["id"]


def test_create_activates_and_calls_org_cp():
    recorder: list = []
    c, _ = make_client(org_client=make_org_client(201, recorder=recorder))
    with c:
        plan_id = _seed_plan_id(c)
        r = c.post("/v1/organizations", json={"name": "Acme Inc", "plan_id": plan_id}, headers=H)
        assert r.status_code == 201
        org = r.json()
        assert org["status"] == "active"
        # Org CP was called once with the SAME org id.
        assert len(recorder) == 1
        sent = json.loads(recorder[0].content)
        assert sent["id"] == org["id"] and sent["name"] == "Acme Inc"


def test_org_cp_failure_marks_failed_502():
    c, _ = make_client(org_client=make_org_client(unavailable=True))
    with c:
        plan_id = _seed_plan_id(c)
        r = c.post("/v1/organizations", json={"name": "Doomed", "plan_id": plan_id}, headers=H)
        assert r.status_code == 502 and r.json()["error"]["type"] == "upstream_error"
        # The org is durably persisted as `failed` (re-issuable), not rolled away or left provisioning.
        orgs = c.get("/v1/organizations", headers=H).json()
        doomed = next(o for o in orgs if o["name"] == "Doomed")
        assert doomed["status"] == "failed"


def test_unknown_plan_422_no_org_cp_call():
    recorder: list = []
    c, _ = make_client(org_client=make_org_client(201, recorder=recorder))
    with c:
        r = c.post(
            "/v1/organizations", json={"name": "Ghost", "plan_id": str(uuid.uuid4())}, headers=H
        )
        assert r.status_code == 422
        assert recorder == []  # Org CP not called for an invalid plan
        # no org row created
        assert all(o["name"] != "Ghost" for o in c.get("/v1/organizations", headers=H).json())


def test_duplicate_name_409_no_phantom():
    recorder: list = []
    c, _ = make_client(org_client=make_org_client(201, recorder=recorder))
    with c:
        plan_id = _seed_plan_id(c)
        first = c.post("/v1/organizations", json={"name": "Twin", "plan_id": plan_id}, headers=H)
        assert first.status_code == 201
        recorder.clear()
        dup = c.post("/v1/organizations", json={"name": "Twin", "plan_id": plan_id}, headers=H)
        assert dup.status_code == 409
        assert recorder == []  # no Org CP call on the conflict
        # exactly one "Twin" org, and it is active (no phantom provisioning row)
        twins = [o for o in c.get("/v1/organizations", headers=H).json() if o["name"] == "Twin"]
        assert len(twins) == 1 and twins[0]["status"] == "active"


def test_retry_reactivates_a_failed_org():
    c, engine = make_client(org_client=make_org_client(unavailable=True))
    with c:
        plan_id = _seed_plan_id(c)
        c.post("/v1/organizations", json={"name": "Retry Me", "plan_id": plan_id}, headers=H)
        # The create call itself 502s; the org is still persisted as `failed` (see
        # test_org_cp_failure_marks_failed_502) — fetch its id via the list.
        org_id = next(o["id"] for o in c.get("/v1/organizations", headers=H).json() if o["name"] == "Retry Me")

    # Reopen the same DB with an Org CP client that now succeeds.
    recorder: list = []
    c2, _ = make_client(engine=engine, org_client=make_org_client(200, recorder=recorder))
    with c2:
        r = c2.post(f"/v1/organizations/{org_id}/retry", headers=H)
        assert r.status_code == 200
        assert r.json()["status"] == "active" and r.json()["retryable"] is False
        # Retry re-issues the SAME org id to Org CP's idempotent init.
        assert len(recorder) == 1
        assert json.loads(recorder[0].content)["id"] == org_id


def test_retry_still_failing_stays_failed_and_502s():
    c, engine = make_client(org_client=make_org_client(unavailable=True))
    with c:
        plan_id = _seed_plan_id(c)
        c.post("/v1/organizations", json={"name": "Still Broken", "plan_id": plan_id}, headers=H)
        org_id = next(
            o["id"] for o in c.get("/v1/organizations", headers=H).json() if o["name"] == "Still Broken"
        )

    c2, _ = make_client(engine=engine, org_client=make_org_client(unavailable=True))
    with c2:
        r = c2.post(f"/v1/organizations/{org_id}/retry", headers=H)
        assert r.status_code == 502 and r.json()["error"]["type"] == "upstream_error"
        assert c2.get(f"/v1/organizations/{org_id}", headers=H).json()["status"] == "failed"


def test_retry_rejects_a_non_failed_org():
    c, _ = make_client(org_client=make_org_client(201))
    with c:
        plan_id = _seed_plan_id(c)
        org = c.post("/v1/organizations", json={"name": "Healthy", "plan_id": plan_id}, headers=H).json()
        assert org["status"] == "active"
        r = c.post(f"/v1/organizations/{org['id']}/retry", headers=H)
        assert r.status_code == 409 and r.json()["error"]["type"] == "conflict"


def test_retry_absent_org_404():
    c, _ = make_client()
    with c:
        r = c.post(f"/v1/organizations/{uuid.uuid4()}/retry", headers=H)
        assert r.status_code == 404


def test_org_creation_notifies_billing():
    recorder: list = []
    c, _ = make_client(billing_client=make_billing_client(recorder=recorder))
    with c:
        plan_id = _priced_plan_id(c)
        r = c.post("/v1/organizations", json={"name": "Billed Co", "plan_id": plan_id}, headers=H)
        assert r.status_code == 201
        org = r.json()
        assert len(recorder) == 1
        sent = json.loads(recorder[0].content)
        assert sent["org_id"] == org["id"] and sent["stripe_price_id"] == "price_abc"


def test_billing_outage_does_not_fail_org_creation():
    c, _ = make_client(billing_client=make_billing_client(unavailable=True))
    with c:
        plan_id = _priced_plan_id(c)
        r = c.post("/v1/organizations", json={"name": "Resilient Co", "plan_id": plan_id}, headers=H)
        assert r.status_code == 201
        assert r.json()["status"] == "active"


def test_retry_success_notifies_billing():
    c, engine = make_client(org_client=make_org_client(unavailable=True))
    with c:
        plan_id = _priced_plan_id(c)
        c.post("/v1/organizations", json={"name": "Retry Billed", "plan_id": plan_id}, headers=H)
        org_id = next(
            o["id"] for o in c.get("/v1/organizations", headers=H).json()
            if o["name"] == "Retry Billed"
        )

    recorder: list = []
    c2, _ = make_client(
        engine=engine,
        org_client=make_org_client(200),
        billing_client=make_billing_client(recorder=recorder),
    )
    with c2:
        r = c2.post(f"/v1/organizations/{org_id}/retry", headers=H)
        assert r.status_code == 200 and r.json()["status"] == "active"
        assert len(recorder) == 1
        assert json.loads(recorder[0].content)["org_id"] == org_id
