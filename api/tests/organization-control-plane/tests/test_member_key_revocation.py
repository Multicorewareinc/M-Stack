"""Member removal revokes the member's API keys (SP-03, add-member-removal-key-revocation, ref §10).
DELETE /v1/users/{id} is service-bearer + org-header scoped; uses the ASGI idiom for shared-engine
seeding."""

from __future__ import annotations

import uuid

import fakeredis.aioredis
from conftest import asgi_client, init_schema, make_auth_app, mg_headers, org_headers

from modules.api_keys import cache, keygen


async def _seed_user_with_keys(sessionmaker, *, n_keys=2, org_id=None):
    from modules.api_keys.models import ApiKey
    from modules.organizations.models import Organization
    from modules.users.models import User

    org_id = org_id or uuid.uuid4()
    async with sessionmaker() as s:
        if await s.get(Organization, org_id) is None:
            s.add(Organization(id=org_id, name="Acme", status="active"))
        u = User(organization_id=org_id, username=f"u{uuid.uuid4().hex[:8]}", email="u@a.com")
        s.add(u)
        await s.flush()
        hashes = []
        for _ in range(n_keys):
            raw, prefix, kh = keygen.generate_raw_key()
            s.add(ApiKey(org_id=org_id, owner_id=u.id, name="k", key_hash=kh, prefix=prefix))
            hashes.append(kh)
        await s.commit()
        return u.id, org_id, hashes


async def test_delete_user_revokes_keys(monkeypatch):
    app, engine = make_auth_app()
    await init_schema(engine)
    monkeypatch.setattr(cache, "get_redis", lambda settings: fakeredis.aioredis.FakeRedis())
    uid, org_id, hashes = await _seed_user_with_keys(app.state.sessionmaker, n_keys=2)
    async with asgi_client(app) as c:
        r = await c.delete(f"/v1/users/{uid}", headers=org_headers(org_id))
    assert r.status_code == 204
    from modules.api_keys.models import ApiKey
    from modules.users.models import User

    async with app.state.sessionmaker() as s:
        assert await s.get(User, uid) is None  # user gone
        for kh in hashes:
            row = await s.scalar(__import__("sqlalchemy").select(ApiKey).where(ApiKey.key_hash == kh))
            assert row is not None and row.revoked_at is not None  # every key revoked, atomically


async def test_removed_owner_keys_report_revoked(monkeypatch):
    app, engine = make_auth_app()
    await init_schema(engine)
    monkeypatch.setattr(cache, "get_redis", lambda settings: fakeredis.aioredis.FakeRedis())
    uid, org_id, hashes = await _seed_user_with_keys(app.state.sessionmaker, n_keys=1)
    async with asgi_client(app) as c:
        await c.delete(f"/v1/users/{uid}", headers=org_headers(org_id))
        v = await c.get("/internal/v1/api-keys/verify", params={"hash": hashes[0]}, headers=mg_headers())
    assert v.status_code == 200
    body = v.json()
    assert body["revoked_at"] is not None and body["owner_active"] is False  # dead + owner gone


async def test_delete_user_no_keys_noop(monkeypatch):
    app, engine = make_auth_app()
    await init_schema(engine)
    monkeypatch.setattr(cache, "get_redis", lambda settings: fakeredis.aioredis.FakeRedis())
    uid, org_id, _ = await _seed_user_with_keys(app.state.sessionmaker, n_keys=0)
    # another org's key must be untouched
    other_uid, other_org, other_hashes = await _seed_user_with_keys(app.state.sessionmaker, n_keys=1)
    async with asgi_client(app) as c:
        r = await c.delete(f"/v1/users/{uid}", headers=org_headers(org_id))
    assert r.status_code == 204
    from modules.api_keys.models import ApiKey

    async with app.state.sessionmaker() as s:
        other = await s.scalar(__import__("sqlalchemy").select(ApiKey).where(ApiKey.key_hash == other_hashes[0]))
    assert other.revoked_at is None  # other org's key untouched


async def test_suspend_user_evicts_keys(monkeypatch):
    # Deactivating a user must evict their keys from the verify cache promptly (not TTL-bounded).
    app, engine = make_auth_app()
    await init_schema(engine)
    fake = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(cache, "get_redis", lambda settings: fake)
    uid, org_id, hashes = await _seed_user_with_keys(app.state.sessionmaker, n_keys=1)
    await fake.set(cache.cache_key(hashes[0]), "cached")  # pretend the key was verified & cached
    async with asgi_client(app) as c:
        r = await c.patch(f"/v1/users/{uid}", json={"status": "suspended"}, headers=org_headers(org_id))
    assert r.status_code == 200 and r.json()["status"] == "suspended"
    assert await fake.get(cache.cache_key(hashes[0])) is None  # evicted post-commit


async def test_reactivate_or_profile_edit_does_not_evict(monkeypatch):
    # A non-deactivating edit (e.g. status active / display_name) must NOT evict the cache.
    app, engine = make_auth_app()
    await init_schema(engine)
    fake = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(cache, "get_redis", lambda settings: fake)
    uid, org_id, hashes = await _seed_user_with_keys(app.state.sessionmaker, n_keys=1)
    await fake.set(cache.cache_key(hashes[0]), "cached")
    async with asgi_client(app) as c:
        r = await c.patch(f"/v1/users/{uid}", json={"display_name": "Renamed"}, headers=org_headers(org_id))
    assert r.status_code == 200
    assert await fake.get(cache.cache_key(hashes[0])) == b"cached"  # untouched


async def test_delete_eviction_best_effort(monkeypatch):
    from redis.exceptions import RedisError

    app, engine = make_auth_app()
    await init_schema(engine)

    def boom(settings):
        class _Boom:
            async def delete(self, *a):
                raise RedisError("down")

        return _Boom()

    monkeypatch.setattr(cache, "get_redis", boom)
    uid, org_id, _ = await _seed_user_with_keys(app.state.sessionmaker, n_keys=1)
    async with asgi_client(app) as c:
        r = await c.delete(f"/v1/users/{uid}", headers=org_headers(org_id))
    assert r.status_code == 204  # eviction fault swallowed; committed delete not turned into a 5xx
