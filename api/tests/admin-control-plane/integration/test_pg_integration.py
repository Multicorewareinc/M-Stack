"""Postgres integration test (AD-07) — the ONE place the asyncpg driver, the Alembic-applied
schema, and the DB-enforced ON DELETE RESTRICT actually execute. It is NOT part of the offline
`testpaths` (that suite runs on aiosqlite); it runs only against a real Postgres.

Preconditions (done by the namespaced harness before this runs):
  1. A Postgres reachable at $ADMIN_PG_DSN (asyncpg DSN, e.g. postgresql://user:pass@host:5432/db).
  2. `alembic upgrade head` already applied against that database.

Run: `python -m pytest api/tests/admin-control-plane/integration -q` with ADMIN_PG_DSN set.
Skips (does not fail) when ADMIN_PG_DSN is absent, so it never breaks the offline suite.
"""

from __future__ import annotations

import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")

DSN = os.environ.get("ADMIN_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="ADMIN_PG_DSN not set — Postgres integration only")


async def test_asyncpg_crud_and_fk_restrict():
    conn = await asyncpg.connect(DSN)
    try:
        plan_id = uuid.uuid4()
        org_id = uuid.uuid4()

        # Migration applied by Alembic must have created both tables.
        await conn.execute(
            "INSERT INTO plans (id, name, tpm, rpm, quota_monthly_tokens, is_default, "
            "created_at, updated_at) VALUES ($1,$2,0,0,0,false,now(),now())",
            plan_id, f"pg-plan-{plan_id}",
        )
        await conn.execute(
            "INSERT INTO organizations (id, name, slug, plan_id, status, created_at, updated_at) "
            "VALUES ($1,$2,$3,$4,'active',now(),now())",
            org_id, f"pg-org-{org_id}", f"pg-org-{org_id}", plan_id,
        )

        # asyncpg CRUD read-back.
        got = await conn.fetchval("SELECT name FROM organizations WHERE id=$1", org_id)
        assert got == f"pg-org-{org_id}"

        # DB-enforced ON DELETE RESTRICT: deleting the referenced plan must be rejected.
        with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
            await conn.execute("DELETE FROM plans WHERE id=$1", plan_id)

        # Cleanup (org first, then plan) leaves the DB as we found it.
        await conn.execute("DELETE FROM organizations WHERE id=$1", org_id)
        await conn.execute("DELETE FROM plans WHERE id=$1", plan_id)
    finally:
        await conn.close()


async def test_plans_stripe_price_id_nullable_and_settable():
    conn = await asyncpg.connect(DSN)
    try:
        plan_id = uuid.uuid4()

        # Migration 0005 must have added a nullable stripe_price_id column.
        await conn.execute(
            "INSERT INTO plans (id, name, tpm, rpm, quota_monthly_tokens, is_default, "
            "created_at, updated_at) VALUES ($1,$2,0,0,0,false,now(),now())",
            plan_id, f"pg-plan-noprice-{plan_id}",
        )
        assert await conn.fetchval(
            "SELECT stripe_price_id FROM plans WHERE id=$1", plan_id
        ) is None

        await conn.execute(
            "UPDATE plans SET stripe_price_id=$1 WHERE id=$2", "price_pg_test", plan_id
        )
        assert await conn.fetchval(
            "SELECT stripe_price_id FROM plans WHERE id=$1", plan_id
        ) == "price_pg_test"

        await conn.execute("DELETE FROM plans WHERE id=$1", plan_id)
    finally:
        await conn.close()
