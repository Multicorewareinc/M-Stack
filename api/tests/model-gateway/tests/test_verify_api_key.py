"""Gateway API-key verification (SP-02, add-mg-api-key-verification). Uses the in-memory FakeRedis
stub + a MockTransport Org CP verify stub — no network, no fakeredis dependency."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from conftest import FakeRedis, VERIFY_ORG_ID, _verify_record
from main import create_app
from settings import Settings

import keygen

TOKEN = "sk-testtoken123456789"
HASH = keygen.hash_key(TOKEN)


def _build(*, redis=None, org_cp_handler=None, upstream_ok=True):
    redis = redis if redis is not None else FakeRedis()
    if org_cp_handler is None:
        org_cp_handler = lambda req: httpx.Response(200, json=_verify_record())
    up = (lambda req: httpx.Response(200, json={"ok": True})) if upstream_ok else None
    app = create_app(
        Settings(upstream_url="http://up"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(up or (lambda r: httpx.Response(200, json={"ok": True})))),
        redis=redis,
        org_cp_client=httpx.AsyncClient(transport=httpx.MockTransport(org_cp_handler), base_url="http://org-cp"),
    )
    return TestClient(app, raise_server_exceptions=False), redis


def _post(client, token=TOKEN):
    return client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {token}"}, json={"model": "m"})


def test_hash_parity_with_org_cp():
    # The gateway's hash_key must match the Org CP's for the same raw (parity across the duplicated copy).
    assert keygen.hash_key(TOKEN) == hashlib.sha256(TOKEN.encode()).hexdigest()


def test_miss_resolves_via_org_cp_and_caches():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, json=_verify_record())

    client, redis = _build(org_cp_handler=handler)
    assert _post(client).status_code == 200
    assert calls["n"] == 1  # one verify call on the cold miss
    assert redis.store.get(f"apikey:{HASH}") is not None  # back-populated
    assert _post(client).status_code == 200
    assert calls["n"] == 1  # hit — no second Org CP call


def test_miss_caps_ttl_at_remaining_life():
    soon = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    client, redis = _build(org_cp_handler=lambda r: httpx.Response(200, json=_verify_record(expires_at=soon)))
    assert _post(client).status_code == 200
    key = f"apikey:{HASH}"
    assert redis.store.get(key) is not None  # cached
    # The TTL actually passed to Redis's SET ... EX must be capped at the key's remaining life
    # (≈30s here), never the full apikey_cache_ttl_seconds ceiling (300s default) — previously
    # this test only checked the key existed, which would still pass even if the cap were dropped
    # entirely (FakeRedis discarded `ex`).
    ex = redis.ex[key]
    assert ex is not None and 0 < ex <= 30


def test_miss_uses_full_ceiling_when_no_expiry():
    """The complementary case: an api key with NO expires_at gets the full configured TTL
    ceiling, not some accidental cap — pins the two branches of the same cap logic."""
    client, redis = _build(org_cp_handler=lambda r: httpx.Response(200, json=_verify_record(expires_at=None)))
    assert _post(client).status_code == 200
    ex = redis.ex[f"apikey:{HASH}"]
    assert ex == Settings().apikey_cache_ttl_seconds


def test_cache_hit_revalidated_rejects_stale():
    # Pre-seed the cache with a record that says active, but it is actually revoked.
    import json

    redis = FakeRedis()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    redis.store[f"apikey:{HASH}"] = json.dumps(_verify_record(revoked_at=past))
    client, _ = _build(redis=redis, org_cp_handler=lambda r: (_ for _ in ()).throw(AssertionError("must not call Org CP on a hit")))
    assert _post(client).status_code == 401  # re-validation catches the revoked snapshot


def test_invalid_keys_uniform_401():
    now = datetime.now(timezone.utc)
    cases = {
        "unknown": lambda r: httpx.Response(404),
        "revoked": lambda r: httpx.Response(200, json=_verify_record(revoked_at=(now - timedelta(hours=1)).isoformat())),
        "expired": lambda r: httpx.Response(200, json=_verify_record(expires_at=(now - timedelta(hours=1)).isoformat())),
        "owner_inactive": lambda r: httpx.Response(200, json=_verify_record(owner_active=False)),
    }
    bodies = set()
    for handler in cases.values():
        client, _ = _build(org_cp_handler=handler)
        r = _post(client)
        assert r.status_code == 401
        bodies.add(r.text)
    assert len(bodies) == 1  # every invalid key returns the identical opaque body (no enumeration)


def test_principal_is_org_id():
    """The event/policy principal is the resolved ORG, not the raw bearer token — asserted here
    directly (not just "elsewhere") by capturing what the policy chain actually received."""
    import json as _json

    policy_calls = []

    def policy_handler(req):
        policy_calls.append(_json.loads(req.content))
        return httpx.Response(200, json={"decision": "allow"})

    redis = FakeRedis()
    app = create_app(
        Settings(upstream_url="http://up", policy_endpoints="http://policy/check"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))),
        redis=redis,
        org_cp_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json=_verify_record(org_id=VERIFY_ORG_ID))
            ),
            base_url="http://org-cp",
        ),
        policy_client=httpx.AsyncClient(transport=httpx.MockTransport(policy_handler)),
    )
    client = TestClient(app, raise_server_exceptions=False)
    assert _post(client).status_code == 200
    assert len(policy_calls) == 1
    assert policy_calls[0]["principal"] == VERIFY_ORG_ID  # the resolved org, never the raw token


def test_policy_payload_excludes_attribution_fields():
    """api_key_id/owner_id are audit/billing-attribution fields only (ADR-028) — they must
    never reach the policy chain's ctx, which stays {principal, model, path}."""
    import json as _json

    policy_calls = []

    def policy_handler(req):
        policy_calls.append(_json.loads(req.content))
        return httpx.Response(200, json={"decision": "allow"})

    redis = FakeRedis()
    app = create_app(
        Settings(upstream_url="http://up", policy_endpoints="http://policy/check"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))),
        redis=redis,
        org_cp_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_verify_record())),
            base_url="http://org-cp",
        ),
        policy_client=httpx.AsyncClient(transport=httpx.MockTransport(policy_handler)),
    )
    client = TestClient(app, raise_server_exceptions=False)
    assert _post(client).status_code == 200
    assert len(policy_calls) == 1
    assert "api_key_id" not in policy_calls[0] and "owner_id" not in policy_calls[0]


def test_missing_bearer_401():
    client, _ = _build()
    r = client.post("/v1/chat/completions", json={"model": "m"})
    assert r.status_code == 401


def test_org_cp_unreachable_on_miss_503():
    def boom(req):
        raise httpx.ConnectError("org cp down", request=req)

    client, _ = _build(org_cp_handler=boom)
    assert _post(client).status_code == 503  # fail-closed, never admit an unverified key


def test_redis_down_degrades_to_direct_verify():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, json=_verify_record())

    client, _ = _build(redis=FakeRedis(boom=True), org_cp_handler=handler)
    assert _post(client).status_code == 200  # Valkey fault → direct verify, no 5xx
    assert calls["n"] == 1
    assert _post(client).status_code == 200
    assert calls["n"] == 2  # no cache available, so each request re-verifies (degraded, not broken)
