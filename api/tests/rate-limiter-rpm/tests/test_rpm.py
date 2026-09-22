"""RPM logic (ADR-013 async design): `/check` is READ-ONLY; counting happens via `count()`
(driven by the backbone consumer). Redis is an injected in-memory fake (conftest.FakeRedis).

Two halves are tested directly on the service:
  - count(...)  -> INCRs the per-scope counters (dedupe on request_id)
  - check(...)  -> reads the counters and returns allow/deny (+ budget fields)
Plus a couple of HTTP smoke tests for the /check seam and fail-open.
"""

import json

import pytest

from conftest import FakeRedis


async def _count(service, n, *, principal="u1", model="m1", rid_prefix="r"):
    """Count n distinct requests (unique request_id each so dedupe doesn't collapse them)."""
    for i in range(n):
        await service.count(principal, model, "/v1/chat/completions", f"{rid_prefix}{i}")


# --- counting (async write path) -------------------------------------------

async def test_count_increments_then_check_allows_under_limit(service_factory):
    svc, _ = service_factory(rate_limits=json.dumps({"user_default": 5}))
    await _count(svc, 2)  # 2 requests counted
    body = await svc.check("u1", "m1", "/v1/chat/completions")
    assert body["decision"] == "allow"
    assert body["limit"] == 5 and body["remaining"] == 3  # 5 - 2


async def test_check_denies_at_limit(service_factory):
    svc, _ = service_factory(rate_limits=json.dumps({"user_default": 3}))
    await _count(svc, 3)  # counter now at the limit
    body = await svc.check("u1", "m1", "/v1/chat/completions")
    assert body["decision"] == "deny"
    assert body["status"] == 429 and body["retry_after"] > 0
    assert "user" in body["reason"]
    assert body["limit"] == 3 and body["remaining"] == 0


async def test_check_read_only_does_not_count(service_factory):
    # Calling /check repeatedly must NOT advance the counter (unlike the old sync design).
    svc, fake = service_factory(rate_limits=json.dumps({"user_default": 1}))
    for _ in range(5):
        assert (await svc.check("u1", "m1", "/v1/chat/completions"))["decision"] == "allow"
    assert fake.store == {}  # nothing written by check()


async def test_dedupe_same_request_id_counts_once(service_factory):
    svc, _ = service_factory(rate_limits=json.dumps({"user_default": 5}))
    for _ in range(4):
        await svc.count("u1", "m1", "/v1/chat/completions", "same-id")  # replays
    body = await svc.check("u1", "m1", "/v1/chat/completions")
    assert body["remaining"] == 4  # counted once (5 - 1), not four times


async def test_most_restrictive_scope_reported(service_factory):
    svc, _ = service_factory(rate_limits=json.dumps({"user_default": 100, "model_default": 2}))
    await _count(svc, 1)  # both scopes now at 1
    body = await svc.check("u1", "m1", "/v1/chat/completions")
    assert body["decision"] == "allow"
    assert body["limit"] == 2 and body["remaining"] == 1  # model scope (tighter), not user (100/99)


async def test_deny_reports_denied_scope(service_factory):
    # user_default=5 (not over) + model_default=1 (over) -> deny must name the model scope.
    svc, _ = service_factory(rate_limits=json.dumps({"user_default": 5, "model_default": 1}))
    await _count(svc, 2)  # user=2 (<5), model=2 (>=1)
    body = await svc.check("u1", "m1", "/v1/chat/completions")
    assert body["decision"] == "deny"
    assert body["reason"] == "model rpm exceeded"
    assert body["limit"] == 1 and body["remaining"] == 0


async def test_user_model_scope_denies_at_limit(service_factory):
    # user+model override applies only to its pair; /check denies at/over the limit (read-only).
    svc, _ = service_factory(rate_limits=json.dumps({"user_model_overrides": {"u1|m1": 1}}))
    await svc.count("u1", "m1", "/v1/chat/completions", "r0")  # count 1 for the pair
    body = await svc.check("u1", "m1", "/v1/chat/completions")
    assert body["decision"] == "deny"
    assert body["reason"] == "user+model rpm exceeded"
    assert body["limit"] == 1 and body["remaining"] == 0
    # a different pair is unaffected by this override
    assert (await svc.check("u1", "m2", "/v1/chat/completions"))["decision"] == "allow"


async def test_unlimited_when_unconfigured(service_factory):
    svc, fake = service_factory(rate_limits="{}")
    await _count(svc, 10)          # no applicable scope => nothing counted
    body = await svc.check("u1", "m1", "/v1/chat/completions")
    assert body == {"decision": "allow"}
    assert fake.store == {}


# --- fail-open (Valkey faults never block) -----------------------------------

async def test_check_redis_down_fails_open(service_factory):
    svc, _ = service_factory(fail=True, rate_limits=json.dumps({"user_default": 1}))
    assert (await svc.check("u1", "m1", "/v1/chat/completions")) == {"decision": "allow"}


async def test_count_redis_down_is_dropped_not_raised(service_factory):
    svc, _ = service_factory(fail=True, rate_limits=json.dumps({"user_default": 1}))
    await svc.count("u1", "m1", "/v1/chat/completions", "r1")  # must not raise


# --- HTTP /check seam (read-only) -------------------------------------------

def test_check_endpoint_allows_when_no_counts(client_factory):
    # With no counting yet (no consumer in this test build), /check reads zero -> allow.
    client, _ = client_factory(rate_limits=json.dumps({"user_default": 1}))
    r = client.post("/check", json={"principal": "u1", "model": "m1", "path": "/v1/chat/completions"})
    assert r.status_code == 200
    assert r.json()["decision"] == "allow"


def test_check_endpoint_denies_when_counter_seeded(client_factory):
    # Seed the fake Valkey as if the consumer had counted 2 requests, then /check denies.
    import time

    fake = FakeRedis()
    minute = int(time.time()) // 60
    fake.store[f"rl:rpm:user:u1:{minute}"] = 2
    client, _ = client_factory(redis=fake, rate_limits=json.dumps({"user_default": 2}))
    r = client.post("/check", json={"principal": "u1", "model": None, "path": "/v1/chat/completions"})
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "deny" and body["status"] == 429 and body["remaining"] == 0
