"""Super-admin fixed-bearer auth on /v1 (ops endpoints stay open)."""

from __future__ import annotations

from conftest import auth_headers


def test_missing_bearer_401(client):
    r = client.get("/v1/plans")  # no Authorization header
    assert r.status_code == 401
    assert r.json()["error"]["type"] == "unauthorized"


def test_wrong_key_401(client):
    r = client.get("/v1/plans", headers=auth_headers("wrong-key"))
    assert r.status_code == 401
    assert r.json()["error"]["type"] == "unauthorized"


def test_correct_key_ok(client):
    r = client.get("/v1/plans", headers=auth_headers())
    assert r.status_code == 200
