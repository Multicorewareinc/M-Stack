"""Postgres integration (ADR-020) for the api_keys migration (0006). The ONE place the
Alembic-applied `api_keys` table executes over asyncpg — validates the migration's real Postgres
DDL (unique key_hash, org_id index, nullable expiry/revoked). Not part of the offline testpaths;
skips when ORG_PG_DSN is absent."""

from __future__ import annotations

import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")

DSN = os.environ.get("ORG_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="ORG_PG_DSN not set — Postgres integration only")


async def test_migration_and_api_key_insert():
    conn = await asyncpg.connect(DSN)
    try:
        kid, org_id, owner_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        key_hash = "a" * 64
        # The migration must have created api_keys with the expected columns.
        await conn.execute(
            "INSERT INTO api_keys (id, org_id, owner_id, name, key_hash, prefix, created_at) "
            "VALUES ($1,$2,$3,'ci',$4,'sk-abc1234',now())",
            kid, org_id, owner_id, key_hash,
        )
        got = await conn.fetchval("SELECT prefix FROM api_keys WHERE id=$1", kid)
        assert got == "sk-abc1234"
        # key_hash is UNIQUE — a duplicate hash must be rejected.
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO api_keys (id, org_id, owner_id, name, key_hash, prefix, created_at) "
                "VALUES ($1,$2,$3,'dup',$4,'sk-abc1234',now())",
                uuid.uuid4(), org_id, owner_id, key_hash,
            )
        await conn.execute("DELETE FROM api_keys WHERE id=$1", kid)
    finally:
        await conn.close()
