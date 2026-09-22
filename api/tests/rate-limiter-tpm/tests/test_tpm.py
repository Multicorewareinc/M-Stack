"""Covers specs/rate-limiter-tpm/spec.md: /check enforcement, overrides, fail-open,
budget fields, and count()'s dedupe/fail-soft behavior."""

from __future__ import annotations

import time


def _minute() -> int:
    return int(time.time()) // 60


def test_check_denies_at_limit(client_factory):
    client, redis = client_factory(rate_limits='{"user_default":3}')
    redis.store["rl:tpm:user:u1:" + str(_minute())] = 3
    resp = client.post("/check", json={"principal": "u1", "model": None, "path": "/v1/x"})
    body = resp.json()
    assert body["decision"] == "deny"
    assert body["status"] == 429
    assert body["retry_after"] > 0
    assert "user" in body["reason"]
    assert body["limit"] == 3
    assert body["remaining"] == 0


def test_check_read_only_does_not_count(client_factory):
    client, redis = client_factory(rate_limits='{"user_default":1}')
    for _ in range(3):
        resp = client.post("/check", json={"principal": "u1", "model": None, "path": "/v1/x"})
        assert resp.json()["decision"] == "allow"
    assert redis.store == {}


def test_deny_reports_denied_scope(client_factory):
    minute = _minute()
    client, redis = client_factory(rate_limits='{"user_default":100000,"model_default":500}')
    redis.store[f"rl:tpm:model:gpt-4o:{minute}"] = 600
    resp = client.post("/check", json={"principal": "u1", "model": "gpt-4o", "path": "/v1/x"})
    body = resp.json()
    assert body["decision"] == "deny"
    assert "model" in body["reason"]
    assert body["limit"] == 500
    assert body["remaining"] == 0


def test_user_model_scope_denies_at_limit(client_factory):
    minute = _minute()
    client, redis = client_factory(rate_limits='{"user_model_overrides":{"u1|m1":500}}')
    redis.store[f"rl:tpm:user_model:u1|m1:{minute}"] = 500
    denied = client.post("/check", json={"principal": "u1", "model": "m1", "path": "/v1/x"})
    assert denied.json()["decision"] == "deny"
    assert "user+model" in denied.json()["reason"]
    allowed = client.post("/check", json={"principal": "u1", "model": "m2", "path": "/v1/x"})
    assert allowed.json()["decision"] == "allow"


def test_override_raises_and_lowers(client_factory):
    minute = _minute()
    client, redis = client_factory(
        rate_limits='{"user_default":100,"user_overrides":{"vip":1000000,"low":1}}'
    )
    redis.store[f"rl:tpm:user:vip:{minute}"] = 500
    redis.store[f"rl:tpm:user:low:{minute}"] = 1
    vip = client.post("/check", json={"principal": "vip", "model": None, "path": "/v1/x"})
    assert vip.json()["decision"] == "allow"
    low = client.post("/check", json={"principal": "low", "model": None, "path": "/v1/x"})
    assert low.json()["decision"] == "deny"


def test_redis_down_fails_open(client_factory):
    client, _redis = client_factory(fail=True, rate_limits='{"user_default":1}')
    resp = client.post("/check", json={"principal": "u1", "model": None, "path": "/v1/x"})
    assert resp.json() == {"decision": "allow"}


def test_unlimited_when_unconfigured(client_factory):
    client, redis = client_factory(rate_limits="{}")
    for _ in range(5):
        resp = client.post("/check", json={"principal": "u1", "model": "m1", "path": "/v1/x"})
        assert resp.json() == {"decision": "allow"}
    assert redis.store == {}


def test_allow_carries_budget_fields(client_factory):
    minute = _minute()
    client, redis = client_factory(rate_limits='{"user_default":1000}')
    redis.store[f"rl:tpm:user:u1:{minute}"] = 400
    resp = client.post("/check", json={"principal": "u1", "model": None, "path": "/v1/x"})
    body = resp.json()
    assert body == {"decision": "allow", "limit": 1000, "remaining": 600, "reset_after": body["reset_after"]}
    assert 1 <= body["reset_after"] <= 60


async def test_dedupe_same_request_id_counts_once(service_factory):
    service, redis = service_factory(rate_limits='{"user_default":1000}')
    await service.count("u1", "m1", "req-1", 100)
    await service.count("u1", "m1", "req-1", 100)  # replay, same request_id
    minute = _minute()
    assert redis.store[f"rl:tpm:user:u1:{minute}"] == 100  # counted once, not twice


async def test_count_redis_down_is_dropped_not_raised(service_factory):
    service, _redis = service_factory(fail=True, rate_limits='{"user_default":1000}')
    await service.count("u1", "m1", "req-1", 100)  # must not raise
