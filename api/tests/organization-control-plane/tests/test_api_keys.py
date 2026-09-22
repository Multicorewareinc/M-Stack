"""API-key lifecycle (SP-01, add-org-api-key-lifecycle). Drives the app over httpx.ASGITransport on
one event loop (like the auth endpoint tests) so schema-create + seeding + requests share the
injected aiosqlite engine. Covers every scenario's named verify target in the delta spec."""

from __future__ import annotations

import hashlib
import inspect
import uuid
from datetime import datetime, timedelta, timezone

import fakeredis.aioredis
import pytest
from pydantic import SecretStr

from conftest import JWT_SECRET, asgi_client, init_schema, make_auth_app, make_admin_client

from modules.api_keys import cache, keygen
from modules.auth.jwt import encode_access_token

PERM_ID = uuid.uuid4()

CATALOG = [
    {
        "id": str(PERM_ID),
        "resource": "apikey",
        "action": "manage",
        "slug": "apikey.manage",
        "description": None,
        "is_active": True,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
]


def _token(uid, org_id) -> str:
    return encode_access_token(
        sub=str(uid),
        email="u@acme.com",
        org=str(org_id) if org_id is not None else None,
        secret=SecretStr(JWT_SECRET),
        ttl_seconds=3600,
    )


def _headers(uid, org_id) -> dict:
    return {"Authorization": f"Bearer {_token(uid, org_id)}"}


async def _seed_user(sessionmaker, *, org_id=None, with_manage=False, org_status="active"):
    """Insert an Organization + a User (no password needed — token minted directly). When
    with_manage, also create a role holding apikey.manage and assign it. Returns (uid, org_id)."""
    from modules.organizations.models import Organization
    from modules.roles.models import Role, RolePermission, UserRole
    from modules.users.models import User

    org_id = org_id or uuid.uuid4()
    async with sessionmaker() as s:
        if await s.get(Organization, org_id) is None:
            s.add(Organization(id=org_id, name="Acme", status=org_status))
        u = User(organization_id=org_id, username=f"u{uuid.uuid4().hex[:8]}", email="u@acme.com")
        s.add(u)
        await s.flush()
        if with_manage:
            role = Role(organization_id=org_id, name="key-admin")
            s.add(role)
            await s.flush()
            s.add(RolePermission(role_id=role.id, permission_id=PERM_ID))
            s.add(UserRole(user_id=u.id, role_id=role.id))
        await s.commit()
        return u.id, org_id


def _app(*, catalog=CATALOG, admin_unavailable=False):
    admin = make_admin_client(
        known_permission_ids=[PERM_ID], permission_catalog=catalog, unavailable=admin_unavailable
    )
    app, engine = make_auth_app(admin_client=admin)
    return app, engine


async def _fresh(sessionmaker_holder):
    pass


# ── data model ────────────────────────────────────────────────────────────────────────────────

async def test_migration_creates_api_keys_table():
    """The ORM model registers on Base so create_all builds api_keys with a unique key_hash."""
    app, engine = _app()
    await init_schema(engine)
    from sqlalchemy import inspect as sa_inspect

    async with engine.begin() as conn:
        names = await conn.run_sync(lambda c: sa_inspect(c).get_table_names())
        cols = await conn.run_sync(lambda c: {col["name"] for col in sa_inspect(c).get_columns("api_keys")})
    assert "api_keys" in names
    assert {"id", "org_id", "owner_id", "name", "key_hash", "prefix", "created_at", "expires_at", "revoked_at"} <= cols


def test_status_precedence_derived():
    from modules.api_keys.schemas import compute_status

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    assert compute_status(revoked_at=past, expires_at=past) == "revoked"  # revoked outranks expired
    assert compute_status(revoked_at=None, expires_at=past) == "expired"
    assert compute_status(revoked_at=None, expires_at=None) == "active"


def test_status_expiry_boundary_inclusive():
    from modules.api_keys.schemas import compute_status

    now = datetime.now(timezone.utc)
    assert compute_status(None, now - timedelta(seconds=1)) == "expired"  # <= now ⇒ expired
    assert compute_status(None, now + timedelta(seconds=1)) == "active"


def test_hash_key_deterministic_sha256_not_argon2():
    raw = "sk-abc123"
    assert keygen.hash_key(raw) == keygen.hash_key(raw)  # deterministic
    assert keygen.hash_key(raw) == hashlib.sha256(raw.encode()).hexdigest()
    assert len(keygen.hash_key(raw)) == 64
    # The key util must not IMPORT the Argon2id password KDF (ADR-028). Check import lines only —
    # the module docstring intentionally names Argon2id to explain the distinction.
    import_lines = [
        ln for ln in inspect.getsource(keygen).splitlines()
        if ln.strip().startswith(("import ", "from "))
    ]
    assert all("password" not in ln and "argon2" not in ln.lower() for ln in import_lines)
    imported = {ln.split()[1] for ln in import_lines}
    assert {"secrets", "hashlib"} <= imported  # stdlib present
    assert imported <= {"secrets", "hashlib", "__future__"}  # nothing else imported


# ── create ────────────────────────────────────────────────────────────────────────────────────

async def test_create_returns_raw_once_stores_hash(caplog):
    app, engine = _app()
    await init_schema(engine)
    uid, org_id = await _seed_user(app.state.sessionmaker, with_manage=True)
    async with asgi_client(app) as c:
        with caplog.at_level("DEBUG"):
            r = await c.post("/api/api-keys", json={"name": "ci"}, headers=_headers(uid, org_id))
    assert r.status_code == 201, r.text
    raw = r.json()["raw_key"]
    assert raw.startswith("sk-")
    assert raw not in caplog.text  # never logged
    # Only the hash is stored; the raw value is nowhere in the row.
    from modules.api_keys.models import ApiKey

    async with app.state.sessionmaker() as s:
        rows = list(await s.scalars(__import__("sqlalchemy").select(ApiKey)))
    assert len(rows) == 1
    assert rows[0].key_hash == keygen.hash_key(raw)
    assert raw not in (rows[0].prefix, rows[0].key_hash, rows[0].name)


async def test_create_scope_from_principal_only():
    app, engine = _app()
    await init_schema(engine)
    uid, org_id = await _seed_user(app.state.sessionmaker, with_manage=True)
    other_org, other_owner = uuid.uuid4(), uuid.uuid4()
    async with asgi_client(app) as c:
        r = await c.post(
            "/api/api-keys",
            json={"name": "k", "org_id": str(other_org), "owner_id": str(other_owner)},
            headers=_headers(uid, org_id),
        )
    assert r.status_code == 201
    body = r.json()
    assert body["org_id"] == str(org_id)  # from principal, body values ignored
    assert body["owner_id"] == str(uid)


# ── list ──────────────────────────────────────────────────────────────────────────────────────

async def test_list_org_scoped_no_raw():
    app, engine = _app()
    await init_schema(engine)
    uid_a, org_a = await _seed_user(app.state.sessionmaker, with_manage=True)
    uid_b, org_b = await _seed_user(app.state.sessionmaker, with_manage=True)
    async with asgi_client(app) as c:
        await c.post("/api/api-keys", json={"name": "a1"}, headers=_headers(uid_a, org_a))
        await c.post("/api/api-keys", json={"name": "a2"}, headers=_headers(uid_a, org_a))
        await c.post("/api/api-keys", json={"name": "b1"}, headers=_headers(uid_b, org_b))
        r = await c.get("/api/api-keys", headers=_headers(uid_a, org_a))
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 2
    assert all("raw_key" not in it for it in items)


async def test_list_allows_plain_member():
    app, engine = _app()
    await init_schema(engine)
    uid, org_id = await _seed_user(app.state.sessionmaker, with_manage=False)  # no apikey.manage
    other_uid, other_org = await _seed_user(app.state.sessionmaker, with_manage=True)
    async with asgi_client(app) as c:
        # Seeded by a DIFFERENT (manage-capable) user in a DIFFERENT org — must not leak into the
        # plain member's list (this is the read-scoping half of the gate; the guard-DIRECTION-only
        # version of this test never created any keys, so it couldn't have caught either a leak or
        # an empty-list false pass).
        created = await c.post(
            "/api/api-keys", json={"name": "mine"}, headers=_headers(uid, org_id)
        )
        assert created.status_code == 403  # plain member still can't CREATE...
        await c.post("/api/api-keys", json={"name": "other-org"}, headers=_headers(other_uid, other_org))
        r = await c.get("/api/api-keys", headers=_headers(uid, org_id))
    assert r.status_code == 200  # ...but CAN list (read-only gate is looser than mutate)
    assert r.json() == []  # sees neither org B's key nor a phantom key of its own


# ── rotate ────────────────────────────────────────────────────────────────────────────────────

async def test_rotate_new_raw_evicts_old_hash(monkeypatch):
    app, engine = _app()
    await init_schema(engine)
    uid, org_id = await _seed_user(app.state.sessionmaker, with_manage=True)
    fake = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(cache, "get_redis", lambda settings: fake)
    async with asgi_client(app) as c:
        created = (await c.post("/api/api-keys", json={"name": "k"}, headers=_headers(uid, org_id))).json()
        old_raw = created["raw_key"]
        old_hash = keygen.hash_key(old_raw)
        await fake.set(cache.cache_key(old_hash), "1")  # pretend the old hash was cached
        r = await c.post(f"/api/api-keys/{created['id']}/rotate", headers=_headers(uid, org_id))
    assert r.status_code == 200
    new = r.json()
    assert new["raw_key"] != old_raw
    assert await fake.get(cache.cache_key(old_hash)) is None  # OLD hash evicted post-commit


async def test_rotate_cross_org_404():
    app, engine = _app()
    await init_schema(engine)
    uid_a, org_a = await _seed_user(app.state.sessionmaker, with_manage=True)
    uid_b, org_b = await _seed_user(app.state.sessionmaker, with_manage=True)
    async with asgi_client(app) as c:
        b_key = (await c.post("/api/api-keys", json={"name": "b"}, headers=_headers(uid_b, org_b))).json()
        r = await c.post(f"/api/api-keys/{b_key['id']}/rotate", headers=_headers(uid_a, org_a))
    assert r.status_code == 404  # not 403 — existence not confirmed


async def test_patch_cross_org_404():
    """A member of org A must not be able to rename/re-expire org B's key (BOLA/IDOR guard on
    PATCH — council review: rotate had this test, patch and revoke did not)."""
    app, engine = _app()
    await init_schema(engine)
    uid_a, org_a = await _seed_user(app.state.sessionmaker, with_manage=True)
    uid_b, org_b = await _seed_user(app.state.sessionmaker, with_manage=True)
    async with asgi_client(app) as c:
        b_key = (await c.post("/api/api-keys", json={"name": "b"}, headers=_headers(uid_b, org_b))).json()
        r = await c.patch(
            f"/api/api-keys/{b_key['id']}", json={"name": "hijacked"}, headers=_headers(uid_a, org_a)
        )
    assert r.status_code == 404  # not 403 — existence not confirmed
    # Confirm the cross-org PATCH had no effect on the row.
    from modules.api_keys.models import ApiKey

    async with app.state.sessionmaker() as s:
        row = await s.get(ApiKey, uuid.UUID(b_key["id"]))
    assert row.name == "b"


async def test_revoke_cross_org_404():
    """A member of org A must not be able to revoke org B's key (BOLA/IDOR guard on DELETE)."""
    app, engine = _app()
    await init_schema(engine)
    uid_a, org_a = await _seed_user(app.state.sessionmaker, with_manage=True)
    uid_b, org_b = await _seed_user(app.state.sessionmaker, with_manage=True)
    async with asgi_client(app) as c:
        b_key = (await c.post("/api/api-keys", json={"name": "b"}, headers=_headers(uid_b, org_b))).json()
        r = await c.delete(f"/api/api-keys/{b_key['id']}", headers=_headers(uid_a, org_a))
    assert r.status_code == 404  # not 403/204 — existence not confirmed, key must survive
    from modules.api_keys.models import ApiKey

    async with app.state.sessionmaker() as s:
        row = await s.get(ApiKey, uuid.UUID(b_key["id"]))
    assert row.revoked_at is None  # cross-org DELETE must not have revoked it


# ── update ────────────────────────────────────────────────────────────────────────────────────

async def test_patch_partial_metadata_only(monkeypatch):
    app, engine = _app()
    await init_schema(engine)
    uid, org_id = await _seed_user(app.state.sessionmaker, with_manage=True)
    fake = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(cache, "get_redis", lambda settings: fake)
    expiry = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    async with asgi_client(app) as c:
        created = (await c.post("/api/api-keys", json={"name": "old", "expires_at": expiry}, headers=_headers(uid, org_id))).json()
        r = await c.patch(f"/api/api-keys/{created['id']}", json={"name": "new"}, headers=_headers(uid, org_id))
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "new"
    assert body["expires_at"] is not None  # untouched
    assert body["prefix"] == created["prefix"]  # secret untouched


async def test_patch_name_not_clearable():
    app, engine = _app()
    await init_schema(engine)
    uid, org_id = await _seed_user(app.state.sessionmaker, with_manage=True)
    async with asgi_client(app) as c:
        created = (await c.post("/api/api-keys", json={"name": "keep"}, headers=_headers(uid, org_id))).json()
        r = await c.patch(f"/api/api-keys/{created['id']}", json={"name": None}, headers=_headers(uid, org_id))
    assert r.status_code == 422


async def test_patch_expires_at_null_clears(monkeypatch):
    app, engine = _app()
    await init_schema(engine)
    uid, org_id = await _seed_user(app.state.sessionmaker, with_manage=True)
    monkeypatch.setattr(cache, "get_redis", lambda settings: fakeredis.aioredis.FakeRedis())
    expiry = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    async with asgi_client(app) as c:
        created = (await c.post("/api/api-keys", json={"name": "k", "expires_at": expiry}, headers=_headers(uid, org_id))).json()
        r = await c.patch(f"/api/api-keys/{created['id']}", json={"expires_at": None}, headers=_headers(uid, org_id))
    assert r.status_code == 200
    assert r.json()["expires_at"] is None


async def test_patch_revoked_conflict(monkeypatch):
    app, engine = _app()
    await init_schema(engine)
    uid, org_id = await _seed_user(app.state.sessionmaker, with_manage=True)
    monkeypatch.setattr(cache, "get_redis", lambda settings: fakeredis.aioredis.FakeRedis())
    async with asgi_client(app) as c:
        created = (await c.post("/api/api-keys", json={"name": "k"}, headers=_headers(uid, org_id))).json()
        await c.delete(f"/api/api-keys/{created['id']}", headers=_headers(uid, org_id))
        r = await c.patch(f"/api/api-keys/{created['id']}", json={"name": "x"}, headers=_headers(uid, org_id))
    assert r.status_code == 409


# ── revoke ────────────────────────────────────────────────────────────────────────────────────

async def test_revoke_idempotent_soft_delete(monkeypatch):
    app, engine = _app()
    await init_schema(engine)
    uid, org_id = await _seed_user(app.state.sessionmaker, with_manage=True)
    monkeypatch.setattr(cache, "get_redis", lambda settings: fakeredis.aioredis.FakeRedis())
    async with asgi_client(app) as c:
        created = (await c.post("/api/api-keys", json={"name": "k"}, headers=_headers(uid, org_id))).json()
        r1 = await c.delete(f"/api/api-keys/{created['id']}", headers=_headers(uid, org_id))
        r2 = await c.delete(f"/api/api-keys/{created['id']}", headers=_headers(uid, org_id))
    assert r1.status_code == 204 and r2.status_code == 204
    from modules.api_keys.models import ApiKey

    async with app.state.sessionmaker() as s:
        row = await s.get(ApiKey, uuid.UUID(created["id"]))
    assert row is not None and row.revoked_at is not None  # soft-delete, row survives


# ── gating ────────────────────────────────────────────────────────────────────────────────────

async def test_mutations_require_apikey_manage():
    app, engine = _app()
    await init_schema(engine)
    uid, org_id = await _seed_user(app.state.sessionmaker, with_manage=False)  # lacks the permission
    async with asgi_client(app) as c:
        h = _headers(uid, org_id)
        assert (await c.post("/api/api-keys", json={"name": "k"}, headers=h)).status_code == 403
        fake_id = uuid.uuid4()
        assert (await c.post(f"/api/api-keys/{fake_id}/rotate", headers=h)).status_code == 403
        assert (await c.patch(f"/api/api-keys/{fake_id}", json={"name": "x"}, headers=h)).status_code == 403
        assert (await c.delete(f"/api/api-keys/{fake_id}", headers=h)).status_code == 403
    from modules.api_keys.models import ApiKey

    async with app.state.sessionmaker() as s:
        assert list(await s.scalars(__import__("sqlalchemy").select(ApiKey))) == []  # nothing created


async def test_gate_fails_closed_when_permission_unresolved():
    # Admin catalog has no apikey.manage entry ⇒ permission id unresolved ⇒ deny even a would-be admin.
    app, engine = _app(catalog=[])
    await init_schema(engine)
    uid, org_id = await _seed_user(app.state.sessionmaker, with_manage=True)
    async with asgi_client(app) as c:
        r = await c.post("/api/api-keys", json={"name": "k"}, headers=_headers(uid, org_id))
    assert r.status_code == 403  # fail-closed, never fail-open


async def test_eviction_best_effort_on_redis_outage(monkeypatch):
    from redis.exceptions import RedisError

    app, engine = _app()
    await init_schema(engine)
    uid, org_id = await _seed_user(app.state.sessionmaker, with_manage=True)

    def boom(settings):
        class _Boom:
            async def delete(self, *a):
                raise RedisError("down")

        return _Boom()

    monkeypatch.setattr(cache, "get_redis", boom)
    async with asgi_client(app) as c:
        created = (await c.post("/api/api-keys", json={"name": "k"}, headers=_headers(uid, org_id))).json()
        # revoke evicts post-commit; a Valkey outage must NOT turn the committed mutation into a 5xx.
        r = await c.delete(f"/api/api-keys/{created['id']}", headers=_headers(uid, org_id))
    assert r.status_code == 204
