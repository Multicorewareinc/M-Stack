"""Roles business logic — tenant-scoped role CRUD, permission composition (validated against the
Admin CP master list, ADR-018/019), and user-role assignment. All DB access here; flush() not
commit() (D3). Cross-org access resolves to 404 (§46)."""

from __future__ import annotations

import uuid

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from errors import ConflictError, NotFoundError, UnprocessableError, UpstreamError
from modules.users.models import User

from .models import Role, RolePermission, UserRole
from .schemas import RoleCreate, RoleUpdate

# ---- Role CRUD (tenant-scoped) ------------------------------------------------------------

async def _flush_unique(session: AsyncSession, name: str) -> None:
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError(
            f"Role name '{name}' already exists in this organization", field="name"
        ) from exc


async def create_role(session: AsyncSession, org_id: uuid.UUID, data: RoleCreate) -> Role:
    role = Role(organization_id=org_id, name=data.name, description=data.description)
    session.add(role)
    await _flush_unique(session, data.name)
    return role


async def list_roles(session: AsyncSession, org_id: uuid.UUID) -> list[Role]:
    result = await session.scalars(
        select(Role).where(Role.organization_id == org_id).order_by(Role.created_at)
    )
    return list(result)


async def get_role(session: AsyncSession, org_id: uuid.UUID, role_id: uuid.UUID) -> Role:
    """Scoped on both ids — a role id from another org resolves to 404 (§46)."""
    role = await session.scalar(
        select(Role).where(Role.id == role_id, Role.organization_id == org_id)
    )
    if role is None:
        raise NotFoundError(f"Role {role_id} not found")
    return role


async def update_role(
    session: AsyncSession, org_id: uuid.UUID, role_id: uuid.UUID, data: RoleUpdate
) -> Role:
    role = await get_role(session, org_id, role_id)
    fields = data.model_dump(exclude_unset=True)
    for key, value in fields.items():
        setattr(role, key, value)
    await _flush_unique(session, role.name)
    return role


async def delete_role(session: AsyncSession, org_id: uuid.UUID, role_id: uuid.UUID) -> None:
    """Delete a role — blocked (409) for a system role (council review fix: `is_system_role` was
    stored and returned in `RoleOut` but never actually enforced anywhere; the default org_admin/
    org_user roles seeded at org-init, ADR-029, could be deleted like any other role, potentially
    leaving an org with no admin-capable role at all)."""
    role = await get_role(session, org_id, role_id)
    if role.is_system_role:
        raise ConflictError(f"Role '{role.name}' is a system role and cannot be deleted")
    # Delete links app-level so cleanup works on aiosqlite too (FK CASCADE is off by default on
    # sqlite; the DB CASCADE is the Postgres backstop). Then delete the role.
    await session.execute(delete(RolePermission).where(RolePermission.role_id == role_id))
    await session.execute(delete(UserRole).where(UserRole.role_id == role_id))
    await session.delete(role)
    await session.flush()


# ---- Permission composition (validate against Admin CP master list) ------------------------

async def _validate_permission(client: httpx.AsyncClient, permission_id: uuid.UUID) -> None:
    """422 if the id is unknown to the master list; 502 if Admin CP is unreachable (fail closed)."""
    try:
        resp = await client.get(f"/internal/v1/permissions/{permission_id}")
    except httpx.HTTPError as exc:
        raise UpstreamError("Admin CP permission validation unavailable") from exc
    if resp.status_code == 404:
        raise UnprocessableError(f"Unknown permission {permission_id}")
    if resp.status_code != 200:
        raise UpstreamError(f"Admin CP returned {resp.status_code} validating permission")


async def set_role_permissions(
    session: AsyncSession,
    client: httpx.AsyncClient,
    org_id: uuid.UUID,
    role_id: uuid.UUID,
    permission_ids: list[uuid.UUID],
) -> None:
    role = await get_role(session, org_id, role_id)  # 404 cross-org
    # Validate ALL ids before writing anything — no partial composition (D2).
    for pid in permission_ids:
        await _validate_permission(client, pid)
    await session.execute(delete(RolePermission).where(RolePermission.role_id == role.id))
    for pid in dict.fromkeys(permission_ids):  # de-dup, preserve order
        session.add(RolePermission(role_id=role.id, permission_id=pid))
    await session.flush()


async def get_role_permission_ids(
    session: AsyncSession, org_id: uuid.UUID, role_id: uuid.UUID
) -> list[uuid.UUID]:
    await get_role(session, org_id, role_id)  # 404 cross-org
    result = await session.scalars(
        select(RolePermission.permission_id).where(RolePermission.role_id == role_id)
    )
    return list(result)


# ---- User-role assignment (tenant-scoped) --------------------------------------------------

async def _require_user(session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> User:
    user = await session.scalar(
        select(User).where(User.id == user_id, User.organization_id == org_id)
    )
    if user is None:
        raise NotFoundError(f"User {user_id} not found")
    return user


async def set_user_roles(
    session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID, role_ids: list[uuid.UUID]
) -> None:
    await _require_user(session, org_id, user_id)  # 404 cross-org user
    for rid in role_ids:
        await get_role(session, org_id, rid)  # 404 if any role is cross-org
    await session.execute(delete(UserRole).where(UserRole.user_id == user_id))
    for rid in dict.fromkeys(role_ids):
        session.add(UserRole(user_id=user_id, role_id=rid))
    await session.flush()


async def get_user_role_ids(
    session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID
) -> list[uuid.UUID]:
    await _require_user(session, org_id, user_id)
    result = await session.scalars(select(UserRole.role_id).where(UserRole.user_id == user_id))
    return list(result)


# ---- Reverse-reference check for Admin CP (ADR-019) --------------------------------------

async def permission_in_use(session: AsyncSession, permission_id: uuid.UUID) -> bool:
    """Whether ANY role, in any org, currently composes this permission id.

    Called by Admin CP before deleting a master-list permission (council review fix): deleting a
    permission that's actively composed into an org's role would silently orphan its
    role_permissions row and leave that permission's effective-permission resolution pointing at
    a dangling id. Platform-wide by design — unlike every other roles/service.py function, this
    one is NOT org-scoped, because the master permission list has no org boundary either."""
    count = await session.scalar(
        select(func.count()).select_from(RolePermission).where(
            RolePermission.permission_id == permission_id
        )
    )
    return bool(count)
