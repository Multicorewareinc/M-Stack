"""Postgres integration (ADR-020) for org-admin user provisioning (ADR-029/AD-02a, SP-02).

Exercises the create + default-role assignment persistence against a real Alembic-migrated Postgres
(the HTTP/RBAC gating is covered offline in tests/test_user_provisioning.py). Runs only with
ORG_PG_DSN set.
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
PW = "N3wUserPass!!x"


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
    from modules.roles.models import Role, RolePermission, UserRole
    from modules.users.models import User

    async with sm() as s:
        user_ids = list(await s.scalars(select(User.id).where(User.organization_id == org_id)))
        role_ids = list(await s.scalars(select(Role.id).where(Role.organization_id == org_id)))
        if user_ids:
            await s.execute(delete(UserRole).where(UserRole.user_id.in_(user_ids)))
        if role_ids:
            await s.execute(delete(RolePermission).where(RolePermission.role_id.in_(role_ids)))
        await s.execute(delete(User).where(User.organization_id == org_id))
        await s.execute(delete(Role).where(Role.organization_id == org_id))
        await s.execute(delete(Organization).where(Organization.id == org_id))
        await s.commit()


async def test_org_admin_create_persists_user_and_role():
    from modules.organizations import service as org_service
    from modules.organizations.schemas import OrgInit
    from modules.roles.models import Role, UserRole
    from modules.users import service as user_service
    from modules.users.models import User
    from modules.users.schemas import UserCreate

    engine = create_async_engine(_async_dsn(DSN))
    sm = async_sessionmaker(engine, expire_on_commit=False)
    org_id = uuid.uuid4()
    admin = _admin_client()
    try:
        async with sm() as s:
            await org_service.init_org(s, OrgInit(id=org_id, name=f"pg-{org_id}"))
            await org_service.seed_default_roles(s, admin, org_id)
            user = await user_service.create_user(
                s, org_id, UserCreate(username="newbie", email="newbie@acme.com", password=PW)
            )
            await user_service.assign_default_role(s, org_id, user.id, "org_user")
            await s.commit()
            new_id = user.id

        async with sm() as s:
            # User persisted in the caller's org, with a password hash (login-capable).
            persisted = await s.get(User, new_id)
            assert persisted is not None
            assert persisted.organization_id == org_id
            assert persisted.password_hash is not None and persisted.must_change_password is True
            # org_user role assignment recorded in user_roles.
            org_user_role_id = await s.scalar(
                select(Role.id).where(Role.organization_id == org_id, Role.name == "org_user")
            )
            assigned = list(
                await s.scalars(select(UserRole.role_id).where(UserRole.user_id == new_id))
            )
            assert assigned == [org_user_role_id]
    finally:
        await _cleanup(sm, org_id)
        await admin.aclose()
        await engine.dispose()
