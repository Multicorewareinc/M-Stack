"""Postgres integration for the seed_admin bootstrap CLI (SP-03). Skips when ADMIN_PG_DSN is absent.

Exercises what aiosqlite cannot: the real `pg_advisory_xact_lock` single-use guarantee (second run
refuses, concurrent runs on SEPARATE connections yield exactly one admin), and the end-to-end seed →
`403 password_change_required` on `/me` → `POST /api/auth/password` → `200` confinement flow against
the SP-02 app on the migrated Postgres schema. Maps tasks 2.2/2.3."""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest

asyncpg = pytest.importorskip("asyncpg")

DSN = os.environ.get("ADMIN_PG_DSN")  # plain asyncpg DSN, e.g. postgresql://admin:admin@host:5432/db
pytestmark = pytest.mark.skipif(not DSN, reason="ADMIN_PG_DSN not set — Postgres integration only")

# SQLAlchemy async URL derived from the plain DSN (asyncpg driver).
def _sa_url() -> str:
    return DSN.replace("postgresql://", "postgresql+asyncpg://", 1)


async def _truncate():
    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute("TRUNCATE refresh_tokens, admin_users CASCADE")
    finally:
        await conn.close()


async def _count() -> int:
    conn = await asyncpg.connect(DSN)
    try:
        return await conn.fetchval("SELECT count(*) FROM admin_users")
    finally:
        await conn.close()


SEED_EMAIL = "seed-admin@site.local"
SEED_PASSWORD = "correct horse battery staple"


async def test_single_use_second_run_refuses():
    await _truncate()
    import seed_admin

    first = await seed_admin.bootstrap_super_admin(SEED_EMAIL, SEED_PASSWORD)
    assert first is not None
    with pytest.raises(seed_admin.AlreadyBootstrappedError):
        await seed_admin.bootstrap_super_admin("other@site.local", SEED_PASSWORD)
    assert await _count() == 1
    await _truncate()


async def test_concurrent_runs_yield_exactly_one_admin():
    await _truncate()
    import seed_admin

    # Each bootstrap builds its OWN engine/connection (sessionmaker=None), so the advisory lock is
    # genuinely contended across connections — the true concurrency guarantee (design D3).
    async def _run(email):
        try:
            await seed_admin.bootstrap_super_admin(email, SEED_PASSWORD)
            return "ok"
        except seed_admin.AlreadyBootstrappedError:
            return "refused"

    results = await asyncio.gather(_run("a@site.local"), _run("b@site.local"))
    assert sorted(results) == ["ok", "refused"]
    assert await _count() == 1
    await _truncate()


async def test_seeded_admin_confined_until_rotation():
    await _truncate()
    import seed_admin
    from main import create_app
    from settings import Settings

    JWT_SECRET = "test-jwt-secret-0123456789abcdef0123456789"

    # Seed via the CLI core, then drive the real app against the same Postgres.
    await seed_admin.bootstrap_super_admin(SEED_EMAIL, SEED_PASSWORD)

    app = create_app(Settings(database_url=_sa_url(), jwt_secret=JWT_SECRET, admin_api_key="k"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        login = await c.post(
            "/api/auth/login", json={"email": SEED_EMAIL, "password": SEED_PASSWORD}
        )
        assert login.status_code == 200
        token = login.json()["access_token"]
        assert login.json()["must_change_password"] is True
        auth = {"Authorization": f"Bearer {token}"}

        # Confined: a gated route is 403 password_change_required until rotation.
        me_before = await c.get("/api/auth/me", headers=auth)
        assert me_before.status_code == 403
        assert me_before.json()["error"]["type"] == "password_change_required"

        # Rotate (current = seed password), then the same route succeeds.
        new_password = "a brand new passphrase 99"
        rot = await c.post(
            "/api/auth/password",
            headers=auth,
            json={"current_password": SEED_PASSWORD, "new_password": new_password},
        )
        assert rot.status_code == 204

        relogin = await c.post(
            "/api/auth/login", json={"email": SEED_EMAIL, "password": new_password}
        )
        assert relogin.status_code == 200
        assert relogin.json()["must_change_password"] is False
        auth2 = {"Authorization": f"Bearer {relogin.json()['access_token']}"}
        me_after = await c.get("/api/auth/me", headers=auth2)
        assert me_after.status_code == 200
    await _truncate()
