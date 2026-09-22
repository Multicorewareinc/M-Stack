"""Postgres integration (ADR-020) for Org CP — the ONE place the Alembic-applied `organizations`
table executes over asyncpg. Not part of the offline `testpaths`; runs only against a real
Postgres with the migration applied. Skips when ORG_PG_DSN is absent."""

from __future__ import annotations

import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")

DSN = os.environ.get("ORG_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="ORG_PG_DSN not set — Postgres integration only")


async def test_migration_and_org_crud():
    conn = await asyncpg.connect(DSN)
    try:
        oid = uuid.uuid4()
        # The migration must have created the organizations table.
        await conn.execute(
            "INSERT INTO organizations (id, name, status, settings, created_at, updated_at) "
            "VALUES ($1,$2,'active','{}',now(),now())",
            oid, f"pg-org-{oid}",
        )
        got = await conn.fetchval("SELECT name FROM organizations WHERE id=$1", oid)
        assert got == f"pg-org-{oid}"
        await conn.execute("DELETE FROM organizations WHERE id=$1", oid)
    finally:
        await conn.close()
