"""Pluggable policy-chain seam (docs/policy-plane.md, ADR-004)."""

import json

import httpx

from conftest import VERIFY_ORG_ID

AUTH = {"Authorization": "Bearer test-key"}


def _allow(_req):
    return httpx.Response(200, json={"decision": "allow"})


def _deny(*, status=None, type=None, retry=None):
    def handler(_req):
        body = {"decision": "deny", "reason": "nope"}
        if status is not None:
            body["status"] = status
        if type is not None:
            body["type"] = type
        if retry is not None:
            body["retry_after"] = retry
        return httpx.Response(200, json=body)
    return handler


def _boom(_req):
    raise httpx.ConnectError("policy down")


def test_inert_when_unconfigured(make_policy_client):
    # No endpoints -> the chain is a no-op: policy never called, upstream reached.
    client, up, pol = make_policy_client(_deny(), policy_endpoints="")
    r = client.post("/v1/chat/completions", headers=AUTH, json={"model": "m"})
    assert r.status_code == 200
    assert pol.requests == []
    assert len(up.requests) == 1


def test_allow_proceeds_and_sends_ctx(make_policy_client):
    client, up, pol = make_policy_client(_allow)
    r = client.post("/v1/chat/completions", headers=AUTH, json={"model": "m1"})
    assert r.status_code == 200
    assert len(up.requests) == 1
    ctx = json.loads(pol.requests[0].content)
    assert ctx == {"principal": VERIFY_ORG_ID, "model": "m1", "path": "/v1/chat/completions"}


def test_deny_returns_429_no_forward(make_policy_client):
    client, up, pol = make_policy_client(_deny(retry=12))
    r = client.post("/v1/chat/completions", headers=AUTH, json={"model": "m"})
    assert r.status_code == 429
    assert r.headers.get("Retry-After") == "12"
    assert r.json()["error"]["type"] == "rate_limit_exceeded"
    assert up.requests == []  # upstream never contacted


def test_deny_policy_driven_403(make_policy_client):
    client, up, pol = make_policy_client(_deny(status=403, type="permission_error"))
    r = client.post("/v1/chat/completions", headers=AUTH, json={"model": "m"})
    assert r.status_code == 403
    assert r.json()["error"]["type"] == "permission_error"
    assert up.requests == []


def test_fail_closed_blocks_on_error(make_policy_client):
    client, up, pol = make_policy_client(_boom, policy_fail_mode="closed")
    r = client.post("/v1/chat/completions", headers=AUTH, json={"model": "m"})
    assert r.status_code >= 400  # blocked (503 policy_unavailable)
    assert up.requests == []


def test_fail_open_allows_on_error(make_policy_client):
    client, up, pol = make_policy_client(_boom, policy_fail_mode="open")
    r = client.post("/v1/chat/completions", headers=AUTH, json={"model": "m"})
    assert r.status_code == 200
    assert len(up.requests) == 1


def test_models_endpoint_gated_model_none(make_policy_client):
    client, up, pol = make_policy_client(_allow)
    r = client.get("/v1/models", headers=AUTH)
    assert r.status_code == 200
    ctx = json.loads(pol.requests[0].content)
    assert ctx["principal"] == VERIFY_ORG_ID
    assert ctx["model"] is None
    assert ctx["path"] == "/v1/models"
