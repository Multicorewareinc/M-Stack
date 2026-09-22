"""Postgres integration for Org CP roles — UNIQUE(organization_id, name) over asyncpg after the
Alembic migration. Skips when ORG_PG_DSN is absent."""

from __future__ import annotations

import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")

DSN = os.environ.get("ORG_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="ORG_PG_DSN not set — Postgres integration only")


async def test_roles_unique_per_org():
    conn = await asyncpg.connect(DSN)
    try:
        org_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO organizations (id, name, status, settings, created_at, updated_at) "
            "VALUES ($1,'r-org','active','{}',now(),now())",
            org_id,
        )
        await conn.execute(
            "INSERT INTO roles (id, organization_id, name, is_system_role, created_at, updated_at) "
            "VALUES ($1,$2,'Admin',false,now(),now())",
            uuid.uuid4(), org_id,
        )
        with pytest.raises(asyncpg.exceptions.UniqueViolationError):
            await conn.execute(
                "INSERT INTO roles (id, organization_id, name, is_system_role, created_at, "
                "updated_at) VALUES ($1,$2,'Admin',false,now(),now())",
                uuid.uuid4(), org_id,
            )
        await conn.execute("DELETE FROM roles WHERE organization_id=$1", org_id)
        await conn.execute("DELETE FROM organizations WHERE id=$1", org_id)
    finally:
        await conn.close()
