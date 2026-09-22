"""Postgres integration (ADR-020) for default org-role seeding (ADR-029/AD-04, SP-01).

Exercises `service.seed_default_roles` against a real Alembic-migrated Postgres: correct roles +
bindings marked system, and idempotent convergence on a repeated provision. Not part of the offline
`testpaths`; runs only with ORG_PG_DSN set (a real Postgres with the migration applied).
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

asyncpg = pytest.importorskip("asyncpg")

from sqlalchemy import delete, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

DSN = os.environ.get("ORG_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="ORG_PG_DSN not set — Postgres integration only")

ALL_SLUGS = (
    "users.read", "users.create", "users.update", "users.delete",
    "roles.read", "roles.create", "roles.update", "roles.delete",
    "organizations.read", "organizations.update", "apikey.manage",
)


def _async_dsn(dsn: str) -> str:
    for prefix in ("postgresql://", "postgres://"):
        if dsn.startswith(prefix):
            return "postgresql+asyncpg://" + dsn[len(prefix):]
    return dsn


def _admin_client() -> httpx.AsyncClient:
    catalog = [{"id": str(uuid.uuid5(uuid.NAMESPACE_DNS, s)), "slug": s} for s in ALL_SLUGS]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=catalog)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://admin-cp")


async def _cleanup(sm, org_id) -> None:
    from modules.organizations.models import Organization
    from modules.roles.models import Role, RolePermission

    async with sm() as s:
        role_ids = list(
            await s.scalars(select(Role.id).where(Role.organization_id == org_id))
        )
        if role_ids:
            await s.execute(delete(RolePermission).where(RolePermission.role_id.in_(role_ids)))
        await s.execute(delete(Role).where(Role.organization_id == org_id))
        await s.execute(delete(Organization).where(Organization.id == org_id))
        await s.commit()


async def test_org_init_seeds_default_roles_with_bindings():
    from modules.organizations import service
    from modules.organizations.schemas import OrgInit
    from modules.roles.models import Role, RolePermission

    engine = create_async_engine(_async_dsn(DSN))
    sm = async_sessionmaker(engine, expire_on_commit=False)
    org_id = uuid.uuid4()
    admin = _admin_client()
    try:
        async with sm() as s:
            await service.init_org(s, OrgInit(id=org_id, name=f"pg-{org_id}"))
            await service.seed_default_roles(s, admin, org_id)
            await s.commit()

        async with sm() as s:
            roles = {
                r.name: r
                for r in await s.scalars(select(Role).where(Role.organization_id == org_id))
            }
            assert set(roles) == {"org_admin", "org_user"}
            assert all(r.is_system_role for r in roles.values())
            admin_binds = list(
                await s.scalars(
                    select(RolePermission.permission_id).where(
                        RolePermission.role_id == roles["org_admin"].id
                    )
                )
            )
            user_binds = list(
                await s.scalars(
                    select(RolePermission.permission_id).where(
                        RolePermission.role_id == roles["org_user"].id
                    )
                )
            )
            assert len(admin_binds) == 11 and len(set(admin_binds)) == 11
            assert len(user_binds) == 3 and len(set(user_binds)) == 3
    finally:
        await _cleanup(sm, org_id)
        await admin.aclose()
        await engine.dispose()


async def test_org_init_role_seed_idempotent():
    from modules.organizations import service
    from modules.organizations.schemas import OrgInit
    from modules.roles.models import Role, RolePermission

    engine = create_async_engine(_async_dsn(DSN))
    sm = async_sessionmaker(engine, expire_on_commit=False)
    org_id = uuid.uuid4()
    admin = _admin_client()
    try:
        for _ in range(2):  # provision twice — second run must add nothing
            async with sm() as s:
                await service.init_org(s, OrgInit(id=org_id, name=f"pg-{org_id}"))
                await service.seed_default_roles(s, admin, org_id)
                await s.commit()

        async with sm() as s:
            role_rows = list(
                await s.scalars(select(Role).where(Role.organization_id == org_id))
            )
            assert len(role_rows) == 2  # no duplicate roles
            role_ids = [r.id for r in role_rows]
            # 11 + 3 bindings, no duplicates across the repeated seed
            pairs = set(
                (str(rid), str(pid))
                for rid, pid in await s.execute(
                    select(RolePermission.role_id, RolePermission.permission_id).where(
                        RolePermission.role_id.in_(role_ids)
                    )
                )
            )
            assert len(pairs) == 14
    finally:
        await _cleanup(sm, org_id)
        await admin.aclose()
        await engine.dispose()
