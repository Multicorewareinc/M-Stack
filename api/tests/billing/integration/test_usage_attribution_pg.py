"""Postgres integration test for the usage attribution columns (add-billing-usage-attribution).
Mirrors test_billing_pg.py's preconditions + skip discipline (BILLING_PG_DSN set, `alembic
upgrade head` already applied). Proves migration 0005 added `api_key_id`/`owner_id` as nullable
columns that round-trip over asyncpg.

Run: `python -m pytest api/tests/billing/integration -q` with BILLING_PG_DSN set.
Skips (does not fail) when BILLING_PG_DSN is absent, so it never breaks the offline suite.
"""

from __future__ import annotations

import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")

DSN = os.environ.get("BILLING_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="BILLING_PG_DSN not set — Postgres integration only")


async def test_usage_attribution_columns_round_trip():
    conn = await asyncpg.connect(DSN)
    try:
        # Row WITH attribution — both ids round-trip.
        with_id = uuid.uuid4()
        with_req = f"pg-attr-{uuid.uuid4()}"
        await conn.execute(
            "INSERT INTO usage (id, request_id, principal, api_key_id, owner_id, source, "
            "created_at) VALUES ($1,$2,$3,$4,$5,$6,now())",
            with_id, with_req, "org-1", "key-1", "owner-1", "provider",
        )
        row = await conn.fetchrow(
            "SELECT api_key_id, owner_id FROM usage WHERE id=$1", with_id
        )
        assert row["api_key_id"] == "key-1"
        assert row["owner_id"] == "owner-1"

        # Row WITHOUT attribution — both columns read back NULL (nullable, no default).
        without_id = uuid.uuid4()
        without_req = f"pg-noattr-{uuid.uuid4()}"
        await conn.execute(
            "INSERT INTO usage (id, request_id, principal, source, created_at) "
            "VALUES ($1,$2,$3,$4,now())",
            without_id, without_req, "org-1", "none",
        )
        row = await conn.fetchrow(
            "SELECT api_key_id, owner_id FROM usage WHERE id=$1", without_id
        )
        assert row["api_key_id"] is None
        assert row["owner_id"] is None

        await conn.execute("DELETE FROM usage WHERE id = ANY($1::uuid[])", [with_id, without_id])
    finally:
        await conn.close()
