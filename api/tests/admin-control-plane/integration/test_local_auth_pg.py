"""Postgres integration for Admin CP local auth (Alembic + asyncpg). Skips when ADMIN_PG_DSN absent.

Runs against the migrated schema (0004): asserts admin_users + refresh_tokens exist with the right
columns/defaults, that admin_users' must_change_password/active server_defaults let a raw writer
omit them, that refresh_tokens stores only the hash (no raw-token column), and that the atomic
rotation `UPDATE ... WHERE revoked_at IS NULL` yields exactly one successor under a concurrent
double-rotate. Maps task 9.7.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")

DSN = os.environ.get("ADMIN_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="ADMIN_PG_DSN not set — Postgres integration only")


def _rowcount(status: str) -> int:
    # asyncpg execute() returns a status like "UPDATE 1" — the trailing integer is the row count.
    return int(status.split()[-1])


async def test_migration_columns_defaults_and_rotation():
    conn = await asyncpg.connect(DSN)
    try:
        user_id = uuid.uuid4()
        # admin_users has exactly the org-less column set — NO role, NO organization_id.
        admin_cols = {
            r["column_name"]
            for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns WHERE table_name='admin_users'"
            )
        }
        assert admin_cols == {
            "id", "email", "password_hash", "must_change_password", "active",
            "created_at", "last_login_at",
        }
        assert "role" not in admin_cols and "organization_id" not in admin_cols

        # server_defaults let a raw writer omit must_change_password + active (SP-01 lesson): this
        # insert supplies neither and must succeed, defaulting to false / true.
        await conn.execute(
            "INSERT INTO admin_users (id, email, password_hash, created_at) "
            "VALUES ($1,'la@platform.local',$2, now())",
            user_id, "$argon2id$v=19$m=19456,t=2,p=1$abc$def",
        )
        row = await conn.fetchrow(
            "SELECT must_change_password, active FROM admin_users WHERE id=$1", user_id
        )
        assert row["must_change_password"] is False and row["active"] is True

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

        # ON DELETE CASCADE: deleting the admin removes its refresh tokens.
        await conn.execute("DELETE FROM admin_users WHERE id=$1", user_id)
        remaining = await conn.fetchval(
            "SELECT count(*) FROM refresh_tokens WHERE user_id=$1", user_id
        )
        assert remaining == 0
    finally:
        await conn.close()
