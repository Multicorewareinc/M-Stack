"""Usage proxy — read-only, tenant-scoped, forwards billing's usage aggregates verbatim
(add-org-cp-usage-proxy)."""

from __future__ import annotations

import uuid

from conftest import auth_headers, make_billing_client, make_client, org_headers

H = auth_headers()


def _init_org(client):
    oid = str(uuid.uuid4())
    r = client.post("/internal/v1/organizations", json={"id": oid, "name": "Org"}, headers=H)
    assert r.status_code == 201
    return oid


# --- summary -------------------------------------------------------------------------------


def test_summary_forwards_billing_response_scoped_to_org():
    stub = make_billing_client(summary_body={
        "requests": 42, "total_tokens": 100, "prompt_tokens": 60, "completion_tokens": 40,
        "period": {"start": "2026-01-01", "end": "2026-02-01"},
    })
    c, _ = make_client(billing_client=stub)
    with c:
        org = _init_org(c)
        r = c.get("/v1/usage/summary", headers=org_headers(org))
        assert r.status_code == 200
        assert r.json() == {
            "requests": 42, "total_tokens": 100, "prompt_tokens": 60, "completion_tokens": 40,
            "period": {"start": "2026-01-01", "end": "2026-02-01"},
        }
    assert len(stub.calls) == 1
    assert stub.calls[0]["params"]["org_id"] == org


def test_summary_passes_through_start_end():
    stub = make_billing_client()
    c, _ = make_client(billing_client=stub)
    with c:
        org = _init_org(c)
        r = c.get(
            "/v1/usage/summary",
            params={"start": "2026-01-01", "end": "2026-02-01"},
            headers=org_headers(org),
        )
        assert r.status_code == 200
    assert stub.calls[-1]["params"]["start"] == "2026-01-01"
    assert stub.calls[-1]["params"]["end"] == "2026-02-01"


def test_summary_omits_start_end_when_caller_omits_them():
    stub = make_billing_client()
    c, _ = make_client(billing_client=stub)
    with c:
        org = _init_org(c)
        r = c.get("/v1/usage/summary", headers=org_headers(org))
        assert r.status_code == 200
    assert "start" not in stub.calls[-1]["params"]
    assert "end" not in stub.calls[-1]["params"]
    assert stub.calls[-1]["params"]["org_id"] == org


def test_summary_upstream_failure_is_502():
    c, _ = make_client(billing_client=make_billing_client(unavailable=True))
    with c:
        org = _init_org(c)
        r = c.get("/v1/usage/summary", headers=org_headers(org))
        assert r.status_code == 502 and r.json()["error"]["type"] == "upstream_error"


def test_summary_requires_bearer():
    stub = make_billing_client()
    c, _ = make_client(billing_client=stub)
    with c:
        org = _init_org(c)
        r = c.get("/v1/usage/summary", headers={"X-Organization-Id": org})  # no bearer
        assert r.status_code == 401
    assert len(stub.calls) == 0


# --- timeseries ----------------------------------------------------------------------------


def test_timeseries_forwards_billing_response_scoped_to_org():
    stub = make_billing_client(timeseries_body={
        "period": {"start": "2026-01-01", "end": "2026-01-03"},
        "buckets": [
            {"date": "2026-01-01", "requests": 1, "total_tokens": 10, "prompt_tokens": 6, "completion_tokens": 4},
            {"date": "2026-01-02", "requests": 0, "total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0},
        ],
    })
    c, _ = make_client(billing_client=stub)
    with c:
        org = _init_org(c)
        r = c.get("/v1/usage/timeseries", headers=org_headers(org))
        assert r.status_code == 200
        assert len(r.json()["buckets"]) == 2
    assert stub.calls[-1]["params"]["org_id"] == org


def test_timeseries_upstream_failure_is_502():
    c, _ = make_client(billing_client=make_billing_client(unavailable=True))
    with c:
        org = _init_org(c)
        r = c.get("/v1/usage/timeseries", headers=org_headers(org))
        assert r.status_code == 502 and r.json()["error"]["type"] == "upstream_error"


def test_timeseries_requires_bearer():
    stub = make_billing_client()
    c, _ = make_client(billing_client=stub)
    with c:
        org = _init_org(c)
        r = c.get("/v1/usage/timeseries", headers={"X-Organization-Id": org})  # no bearer
        assert r.status_code == 401
    assert len(stub.calls) == 0


# --- by-user (add-org-cp-usage-breakdown-proxy) --------------------------------------------


def test_by_user_forwards_billing_response_scoped_to_org():
    stub = make_billing_client(by_user_body={
        "period": {"start": "2026-01-01", "end": "2026-02-01"},
        "users": [
            {"owner_id": "u1", "requests": 3, "total_tokens": 30, "prompt_tokens": 18, "completion_tokens": 12},
            {"owner_id": None, "requests": 1, "total_tokens": 5, "prompt_tokens": 3, "completion_tokens": 2},
        ],
    })
    c, _ = make_client(billing_client=stub)
    with c:
        org = _init_org(c)
        r = c.get("/v1/usage/by-user", headers=org_headers(org))
        assert r.status_code == 200
        users = r.json()["users"]
        assert {u["owner_id"] for u in users} == {"u1", None}
    assert stub.calls[-1]["path"] == "/internal/v1/usage/by-user"
    assert stub.calls[-1]["params"]["org_id"] == org


def test_by_user_upstream_failure_is_502():
    c, _ = make_client(billing_client=make_billing_client(unavailable=True))
    with c:
        org = _init_org(c)
        r = c.get("/v1/usage/by-user", headers=org_headers(org))
        assert r.status_code == 502 and r.json()["error"]["type"] == "upstream_error"


def test_by_user_requires_bearer():
    stub = make_billing_client()
    c, _ = make_client(billing_client=stub)
    with c:
        org = _init_org(c)
        r = c.get("/v1/usage/by-user", headers={"X-Organization-Id": org})  # no bearer
        assert r.status_code == 401
    assert len(stub.calls) == 0


# --- by-key (add-org-cp-usage-breakdown-proxy) ---------------------------------------------


def test_by_key_forwards_billing_response_scoped_to_org():
    stub = make_billing_client(by_key_body={
        "period": {"start": "2026-01-01", "end": "2026-02-01"},
        "api_keys": [
            {"api_key_id": "k1", "requests": 2, "total_tokens": 20, "prompt_tokens": 12, "completion_tokens": 8},
        ],
    })
    c, _ = make_client(billing_client=stub)
    with c:
        org = _init_org(c)
        r = c.get("/v1/usage/by-key", headers=org_headers(org))
        assert r.status_code == 200
        assert r.json()["api_keys"][0]["api_key_id"] == "k1"
    assert stub.calls[-1]["path"] == "/internal/v1/usage/by-key"
    assert stub.calls[-1]["params"]["org_id"] == org


def test_by_key_upstream_failure_is_502():
    c, _ = make_client(billing_client=make_billing_client(unavailable=True))
    with c:
        org = _init_org(c)
        r = c.get("/v1/usage/by-key", headers=org_headers(org))
        assert r.status_code == 502 and r.json()["error"]["type"] == "upstream_error"


def test_by_key_requires_bearer():
    stub = make_billing_client()
    c, _ = make_client(billing_client=stub)
    with c:
        org = _init_org(c)
        r = c.get("/v1/usage/by-key", headers={"X-Organization-Id": org})  # no bearer
        assert r.status_code == 401
    assert len(stub.calls) == 0
