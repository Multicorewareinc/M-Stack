"""Postgres integration for Org CP users — the composite UNIQUE(organization_id, email) over
asyncpg after the Alembic migration. Skips when ORG_PG_DSN is absent."""

from __future__ import annotations

import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")

DSN = os.environ.get("ORG_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="ORG_PG_DSN not set — Postgres integration only")


async def test_users_composite_unique():
    conn = await asyncpg.connect(DSN)
    try:
        org_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO organizations (id, name, status, settings, created_at, updated_at) "
            "VALUES ($1,'u-org','active','{}',now(),now())",
            org_id,
        )
        await conn.execute(
            "INSERT INTO users (id, organization_id, username, email, status, metadata, "
            "created_at, updated_at) VALUES ($1,$2,'a','x@acme.com','active','{}',now(),now())",
            uuid.uuid4(), org_id,
        )
        # Same (organization_id, email) must violate the composite UNIQUE constraint.
        with pytest.raises(asyncpg.exceptions.UniqueViolationError):
            await conn.execute(
                "INSERT INTO users (id, organization_id, username, email, status, metadata, "
                "created_at, updated_at) VALUES ($1,$2,'b','x@acme.com','active','{}',now(),now())",
                uuid.uuid4(), org_id,
            )
        # cleanup
        await conn.execute("DELETE FROM users WHERE organization_id=$1", org_id)
        await conn.execute("DELETE FROM organizations WHERE id=$1", org_id)
    finally:
        await conn.close()
