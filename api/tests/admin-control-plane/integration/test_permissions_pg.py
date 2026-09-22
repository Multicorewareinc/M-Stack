"""Postgres integration (AD-07) for the master permission list — the ONE place the
Alembic-applied `permissions` table and the DB-enforced UNIQUE(slug) constraint execute over
asyncpg. Not part of the offline `testpaths` (that runs on aiosqlite); runs only against a real
Postgres with the migration already applied.

Preconditions (done by the namespaced harness before this runs):
  1. A Postgres reachable at $ADMIN_PG_DSN (asyncpg DSN).
  2. `alembic upgrade head` already applied (revisions 0001 + 0002).

Skips (does not fail) when ADMIN_PG_DSN is absent, so it never breaks the offline suite.
"""

from __future__ import annotations

import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")

DSN = os.environ.get("ADMIN_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="ADMIN_PG_DSN not set — Postgres integration only")


async def test_migration_and_unique_slug():
    conn = await asyncpg.connect(DSN)
    try:
        pid = uuid.uuid4()
        slug = f"pg.{pid.hex[:8]}"

        # The migration (0002) must have created the permissions table.
        await conn.execute(
            "INSERT INTO permissions (id, resource, action, slug, description, is_active, "
            "created_at, updated_at) VALUES ($1,'pg','x',$2,NULL,true,now(),now())",
            pid, slug,
        )

        got = await conn.fetchval("SELECT slug FROM permissions WHERE id=$1", pid)
        assert got == slug

        # DB-enforced UNIQUE(slug): a second row with the same slug must be rejected.
        with pytest.raises(asyncpg.exceptions.UniqueViolationError):
            await conn.execute(
                "INSERT INTO permissions (id, resource, action, slug, description, is_active, "
                "created_at, updated_at) VALUES ($1,'pg','y',$2,NULL,true,now(),now())",
                uuid.uuid4(), slug,
            )

        await conn.execute("DELETE FROM permissions WHERE id=$1", pid)
    finally:
        await conn.close()
