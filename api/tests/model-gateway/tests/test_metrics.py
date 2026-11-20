"""Gateway metrics on /metrics: request RED, upstream outcomes, policy decisions, key verify.

Counters are process-global, so every assertion is a delta around the call under test.
"""

import httpx
from prometheus_client import REGISTRY

AUTH = {"Authorization": "Bearer test-key"}


def _value(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_http_requests_are_labelled_by_route_template(make_client):
    client, _ = make_client(lambda _req: httpx.Response(200, json={"ok": True}))
    labels = {"method": "POST", "route": "/v1/chat/completions", "status": "200"}
    before = _value("http_requests_total", **labels)
    assert client.post("/v1/chat/completions", headers=AUTH, json={"model": "m"}).status_code == 200
    assert _value("http_requests_total", **labels) == before + 1


def test_upstream_outcome_by_status_class(make_client):
    client, _ = make_client(lambda _req: httpx.Response(503, json={"error": "busy"}))
    labels = {"model": "default", "upstream_type": "openai", "outcome": "5xx"}
    before = _value("gw_upstream_requests_total", **labels)
    client.post("/v1/chat/completions", headers=AUTH, json={"model": "anything"})
    assert _value("gw_upstream_requests_total", **labels) == before + 1


def test_unrouted_model_names_never_become_labels(make_client):
    """A client-chosen model string must not mint a series."""
    client, _ = make_client(lambda _req: httpx.Response(200, json={"ok": True}))
    client.post("/v1/chat/completions", headers=AUTH, json={"model": "made-up-model-123"})
    assert _value("gw_upstream_requests_total", model="made-up-model-123",
                  upstream_type="openai", outcome="2xx") == 0


def test_upstream_unreachable_is_counted(make_client):
    def down(_req):
        raise httpx.ConnectError("refused")

    client, _ = make_client(down)
    labels = {"model": "default", "upstream_type": "openai", "outcome": "unreachable"}
    before = _value("gw_upstream_requests_total", **labels)
    assert client.post("/v1/chat/completions", headers=AUTH, json={"model": "m"}).status_code == 503
    assert _value("gw_upstream_requests_total", **labels) == before + 1


def test_policy_decisions_are_counted_per_policy(make_policy_client):
    deny = lambda _req: httpx.Response(200, json={"decision": "deny", "reason": "nope"})  # noqa: E731
    client, _, _ = make_policy_client(deny, policy_endpoints="http://rate-limiter-rpm/check")
    labels = {"policy": "rate-limiter-rpm", "decision": "deny"}
    before = _value("gw_policy_decisions_total", **labels)
    assert client.post("/v1/chat/completions", headers=AUTH, json={"model": "m"}).status_code == 429
    assert _value("gw_policy_decisions_total", **labels) == before + 1


def test_an_unreachable_policy_is_counted(make_policy_client):
    def down(_req):
        raise httpx.ConnectError("policy down")

    client, _, _ = make_policy_client(down, policy_endpoints="http://rate-limiter-tpm/check")
    labels = {"policy": "rate-limiter-tpm", "decision": "unavailable"}
    before = _value("gw_policy_decisions_total", **labels)
    client.post("/v1/chat/completions", headers=AUTH, json={"model": "m"})
    assert _value("gw_policy_decisions_total", **labels) == before + 1


def test_api_key_verification_is_counted(make_client):
    client, _ = make_client(lambda _req: httpx.Response(200, json={"ok": True}))
    before = _value("gw_api_key_verify_total", source="org_cp", result="valid")
    client.post("/v1/chat/completions", headers=AUTH, json={"model": "m"})
    assert _value("gw_api_key_verify_total", source="org_cp", result="valid") == before + 1
