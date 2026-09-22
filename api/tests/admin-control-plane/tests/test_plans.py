"""Plans CRUD, unique-name conflict, and the referential delete rules."""

from __future__ import annotations

import uuid

from conftest import auth_headers

H = auth_headers()


def test_create_get_list_patch_delete(client):
    created = client.post("/v1/plans", json={"name": "Team", "tpm": 200, "rpm": 20}, headers=H)
    assert created.status_code == 201
    plan = created.json()
    assert plan["name"] == "Team" and plan["tpm"] == 200 and plan["is_default"] is False

    got = client.get(f"/v1/plans/{plan['id']}", headers=H)
    assert got.status_code == 200 and got.json()["id"] == plan["id"]

    names = [p["name"] for p in client.get("/v1/plans", headers=H).json()]
    assert "Team" in names

    patched = client.patch(f"/v1/plans/{plan['id']}", json={"rpm": 99}, headers=H)
    assert patched.status_code == 200 and patched.json()["rpm"] == 99

    assert client.delete(f"/v1/plans/{plan['id']}", headers=H).status_code == 204
    assert client.get(f"/v1/plans/{plan['id']}", headers=H).status_code == 404


def test_duplicate_name_conflicts(client):
    client.post("/v1/plans", json={"name": "Dup"}, headers=H)
    r = client.post("/v1/plans", json={"name": "Dup"}, headers=H)
    assert r.status_code == 409 and r.json()["error"]["type"] == "conflict"
    assert r.json()["error"]["field"] == "name"


def test_get_absent_404(client):
    r = client.get(f"/v1/plans/{uuid.uuid4()}", headers=H)
    assert r.status_code == 404 and r.json()["error"]["type"] == "not_found"


def test_delete_unused_plan_no_content(client):
    plan = client.post("/v1/plans", json={"name": "Unused"}, headers=H).json()
    assert client.delete(f"/v1/plans/{plan['id']}", headers=H).status_code == 204


def test_delete_referenced_plan_conflicts(client, plan_id):
    # plan_id is a seeded plan; attach an org, then deleting the plan must 409.
    org = client.post("/v1/organizations", json={"name": "Acme", "plan_id": plan_id}, headers=H)
    assert org.status_code == 201
    r = client.delete(f"/v1/plans/{plan_id}", headers=H)
    assert r.status_code == 409 and r.json()["error"]["type"] == "conflict"
    assert client.get(f"/v1/plans/{plan_id}", headers=H).status_code == 200  # not deleted


def test_delete_absent_404(client):
    r = client.delete(f"/v1/plans/{uuid.uuid4()}", headers=H)
    assert r.status_code == 404 and r.json()["error"]["type"] == "not_found"


def test_malformed_body_422(client):
    r = client.post("/v1/plans", json={"tpm": 5}, headers=H)  # missing required name
    assert r.status_code == 422


def test_deactivate_plan_soft_disables_without_deleting(client):
    plan = client.post("/v1/plans", json={"name": "ToDeactivate"}, headers=H).json()
    assert plan["is_active"] is True

    r = client.patch(f"/v1/plans/{plan['id']}", json={"is_active": False}, headers=H)
    assert r.status_code == 200 and r.json()["is_active"] is False
    # Still fully present, not deleted — an org already on it is unaffected.
    assert client.get(f"/v1/plans/{plan['id']}", headers=H).status_code == 200


def test_omitted_limits_default_zero(client):
    created = client.post("/v1/plans", json={"name": "Custom"}, headers=H)
    assert created.status_code == 201
    plan = created.json()
    assert plan["tpm"] == 0 and plan["rpm"] == 0 and plan["quota_monthly_tokens"] == 0
    assert plan["is_default"] is False
    got = client.get(f"/v1/plans/{plan['id']}", headers=H).json()
    assert got["tpm"] == 0 and got["rpm"] == 0 and got["quota_monthly_tokens"] == 0


def test_omitted_stripe_price_id_defaults_null(client):
    created = client.post("/v1/plans", json={"name": "NoPrice"}, headers=H)
    assert created.status_code == 201
    assert created.json()["stripe_price_id"] is None


def test_stripe_price_id_round_trips(client):
    created = client.post(
        "/v1/plans", json={"name": "Priced", "stripe_price_id": "price_abc"}, headers=H
    )
    plan_id = created.json()["id"]
    assert created.json()["stripe_price_id"] == "price_abc"

    patched = client.patch(
        f"/v1/plans/{plan_id}", json={"stripe_price_id": "price_def"}, headers=H
    )
    assert patched.status_code == 200 and patched.json()["stripe_price_id"] == "price_def"
    assert client.get(f"/v1/plans/{plan_id}", headers=H).json()["stripe_price_id"] == "price_def"
