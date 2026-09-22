"""Generic X-RateLimit-* header stamping (ADR-012). Policy replies carry optional
limit/remaining/reset_after; the gateway stamps the most-restrictive on the response via a
pure-ASGI middleware. Uses the stubbed policy chain (make_policy_client)."""

import httpx

AUTH = {"Authorization": "Bearer test-key"}
RL = ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset")


def _reply(**fields):
    def handler(_req):
        return httpx.Response(200, json={"decision": "allow", **fields})
    return handler


def test_allow_stamps_headers(make_policy_client):
    client, up, pol = make_policy_client(_reply(limit=100, remaining=40, reset_after=30))
    r = client.post("/v1/chat/completions", headers=AUTH, json={"model": "m"})
    assert r.status_code == 200
    assert r.headers["x-ratelimit-limit"] == "100"
    assert r.headers["x-ratelimit-remaining"] == "40"
    assert int(r.headers["x-ratelimit-reset"]) > 0


def test_deny_stamps_headers(make_policy_client):
    def deny(_req):
        return httpx.Response(200, json={
            "decision": "deny", "status": 429, "type": "rate_limit_exceeded",
            "retry_after": 30, "limit": 5, "remaining": 0, "reset_after": 30,
        })
    client, up, pol = make_policy_client(deny)
    r = client.post("/v1/chat/completions", headers=AUTH, json={"model": "m"})
    assert r.status_code == 429
    assert r.headers.get("Retry-After") == "30"  # primary 429 signal, still present
    assert r.headers["x-ratelimit-limit"] == "5" and r.headers["x-ratelimit-remaining"] == "0"
    assert up.requests == []  # not forwarded


def test_most_restrictive_wins(make_policy_client):
    def handler(req):
        if req.url.host == "p1":  # ratio 0.5
            return httpx.Response(200, json={"decision": "allow", "limit": 100, "remaining": 50, "reset_after": 10})
        return httpx.Response(200, json={"decision": "allow", "limit": 100, "remaining": 5, "reset_after": 20})  # ratio 0.05
    client, up, pol = make_policy_client(handler, policy_endpoints="http://p1/check,http://p2/check")
    r = client.post("/v1/chat/completions", headers=AUTH, json={"model": "m"})
    assert r.status_code == 200
    assert r.headers["x-ratelimit-remaining"] == "5"  # the tighter (p2) reply wins


def test_unlimited_never_wins(make_policy_client):
    # Mixed: an unlimited (limit 0) reply must never be chosen; the real-limit one is stamped.
    def mixed(req):
        if req.url.host == "p1":
            return httpx.Response(200, json={"decision": "allow", "limit": 0})  # unlimited
        return httpx.Response(200, json={"decision": "allow", "limit": 10, "remaining": 3, "reset_after": 5})
    client, up, pol = make_policy_client(mixed, policy_endpoints="http://p1/check,http://p2/check")
    r = client.post("/v1/chat/completions", headers=AUTH, json={"model": "m"})
    assert r.headers["x-ratelimit-limit"] == "10" and r.headers["x-ratelimit-remaining"] == "3"

    # All unlimited: no headers stamped.
    client2, up2, pol2 = make_policy_client(_reply(limit=0))
    r2 = client2.post("/v1/chat/completions", headers=AUTH, json={"model": "m"})
    assert r2.status_code == 200
    assert not any(h in r2.headers for h in RL)


def test_inert_and_streaming_intact(make_policy_client):
    parts = [b'data: {"x":1}\n\n', b"data: [DONE]\n\n"]
    def upstream(_req):
        async def body():
            for p in parts:
                yield p
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body())
    # Policy returns no rate-limit fields => nothing stamped; streaming stays intact.
    client, up, pol = make_policy_client(_reply(), upstream_handler=upstream)
    r = client.post("/v1/chat/completions", headers=AUTH, json={"model": "m", "stream": True})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    assert r.content == b"".join(parts)  # body intact (pure ASGI touches only start headers)
    assert not any(h in r.headers for h in RL)


def test_malformed_fields_no_500(make_policy_client):
    # A policy returning a non-numeric limit must be skipped, not 500 the gateway.
    client, up, pol = make_policy_client(_reply(limit="lots", remaining="none"))
    r = client.post("/v1/chat/completions", headers=AUTH, json={"model": "m"})
    assert r.status_code == 200
    assert not any(h in r.headers for h in RL)
