"""Postgres integration for Org CP local auth (Alembic + asyncpg). Skips when ORG_PG_DSN is absent.

Runs against the migrated schema (0004): asserts the two new users columns + refresh_tokens exist,
that refresh_tokens stores only the hash (no raw-token column), and that the atomic rotation
`UPDATE ... WHERE revoked_at IS NULL` yields exactly one successor under a concurrent double-rotate.
Maps task 9.7.

`test_migration_columns_and_refresh_token_stores_only_hash` races the two UPDATEs sequentially on
ONE connection — real, but not a genuine concurrency proof (a single connection can't race itself).
`test_concurrent_rotation_yields_exactly_one_successor` (council review fix) races them with
`asyncio.gather` over TWO independent asyncpg connections — real concurrent scheduling against
Postgres's actual per-connection transaction isolation, which is what an offline aiosqlite/
StaticPool harness cannot reproduce (a single shared connection there gives two "concurrent"
sessions the same transaction context, not real isolation — see the note on
`organization-control-plane/tests/test_auth_sessions.py::test_concurrent_refresh_exactly_one_wins`)."""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")

DSN = os.environ.get("ORG_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="ORG_PG_DSN not set — Postgres integration only")


def _rowcount(status: str) -> int:
    # asyncpg execute() returns a status like "UPDATE 1" — the trailing integer is the row count.
    return int(status.split()[-1])


async def test_migration_columns_and_refresh_token_stores_only_hash():
    conn = await asyncpg.connect(DSN)
    try:
        org_id, user_id = uuid.uuid4(), uuid.uuid4()
        await conn.execute(
            "INSERT INTO organizations (id, name, status, settings, created_at, updated_at) "
            "VALUES ($1,'la-org','active','{}',now(),now())",
            org_id,
        )
        # The two new users columns exist and are writable (password_hash nullable,
        # must_change_password NOT NULL).
        await conn.execute(
            "INSERT INTO users (id, organization_id, username, email, status, metadata, "
            "password_hash, must_change_password, created_at, updated_at) "
            "VALUES ($1,$2,'la','la@acme.com','active','{}',$3,true,now(),now())",
            user_id, org_id, "$argon2id$v=19$m=19456,t=2,p=1$abc$def",
        )

        # refresh_tokens stores ONLY the SHA-256 hash — there is no raw-token column.
        cols = {
            r["column_name"]
            for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'refresh_tokens'"
            )
        }
        assert cols == {"id", "user_id", "token_hash", "expires_at", "revoked_at", "created_at"}

        raw = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw.encode("ascii")).hexdigest()
        rt_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO refresh_tokens (id, user_id, token_hash, expires_at, created_at) "
            "VALUES ($1,$2,$3, now() + interval '7 days', now())",
            rt_id, user_id, token_hash,
        )
        # The raw token never appears in the row.
        stored = await conn.fetchval("SELECT token_hash FROM refresh_tokens WHERE id=$1", rt_id)
        assert stored == token_hash and raw not in stored

        # Atomic rotation: the conditional UPDATE ... WHERE revoked_at IS NULL affects the row once;
        # a second identical update (the concurrent loser) affects 0 rows → exactly one successor.
        first = await conn.execute(
            "UPDATE refresh_tokens SET revoked_at=now() WHERE id=$1 AND revoked_at IS NULL", rt_id
        )
        second = await conn.execute(
            "UPDATE refresh_tokens SET revoked_at=now() WHERE id=$1 AND revoked_at IS NULL", rt_id
        )
        assert _rowcount(first) == 1
        assert _rowcount(second) == 0

        # cleanup
        await conn.execute("DELETE FROM refresh_tokens WHERE user_id=$1", user_id)
        await conn.execute("DELETE FROM users WHERE id=$1", user_id)
        await conn.execute("DELETE FROM organizations WHERE id=$1", org_id)
    finally:
        await conn.close()


async def test_concurrent_rotation_yields_exactly_one_successor():
    """Genuine concurrent scheduling over TWO independent connections (real per-connection
    transaction isolation, unlike the offline harness's shared single connection) — races the
    same conditional UPDATE two application-layer callers would race in refresh_session."""
    setup_conn = await asyncpg.connect(DSN)
    org_id, user_id, rt_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        await setup_conn.execute(
            "INSERT INTO organizations (id, name, status, settings, created_at, updated_at) "
            "VALUES ($1,'race-org','active','{}',now(),now())",
            org_id,
        )
        await setup_conn.execute(
            "INSERT INTO users (id, organization_id, username, email, status, metadata, "
            "must_change_password, created_at, updated_at) "
            "VALUES ($1,$2,'race','race@acme.com','active','{}',false,now(),now())",
            user_id, org_id,
        )
        token_hash = hashlib.sha256(secrets.token_urlsafe(32).encode("ascii")).hexdigest()
        await setup_conn.execute(
            "INSERT INTO refresh_tokens (id, user_id, token_hash, expires_at, created_at) "
            "VALUES ($1,$2,$3, now() + interval '7 days', now())",
            rt_id, user_id, token_hash,
        )

        async def _rotate() -> str:
            conn = await asyncpg.connect(DSN)
            try:
                return await conn.execute(
                    "UPDATE refresh_tokens SET revoked_at=now() WHERE id=$1 AND revoked_at IS NULL",
                    rt_id,
                )
            finally:
                await conn.close()

        results = await asyncio.gather(_rotate(), _rotate())
        rowcounts = sorted(_rowcount(r) for r in results)
        assert rowcounts == [0, 1]  # exactly one of the two genuinely-concurrent callers won

        revoked_at = await setup_conn.fetchval(
            "SELECT revoked_at FROM refresh_tokens WHERE id=$1", rt_id
        )
        assert revoked_at is not None  # the row ended up revoked exactly once, not left live
    finally:
        await setup_conn.execute("DELETE FROM refresh_tokens WHERE user_id=$1", user_id)
        await setup_conn.execute("DELETE FROM users WHERE id=$1", user_id)
        await setup_conn.execute("DELETE FROM organizations WHERE id=$1", org_id)
        await setup_conn.close()
