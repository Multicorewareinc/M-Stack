"""RBAC resolution — a read model over users / user_roles / roles / role_permissions. No models
of its own, no writes. Effective permissions are the union of permission ids across a user's
roles (§29, §47). Everything is tenant-scoped; a cross-org user resolves to 404 (§46).

Also owns `fetch_permission_id_by_slug` — the ONE httpx call site for resolving an Admin
CP-owned permission slug to its UUID (council review fix: this used to be duplicated directly in
both `rbac/dependencies.py` and `api_keys/dependencies.py`, each making its own outbound call from
a dependency module — against the "httpx only in service.py" convention. Both call sites now
delegate here.)."""

from __future__ import annotations

import logging
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from errors import NotFoundError
from modules.roles.models import Role, RolePermission, UserRole
from modules.users.models import User

log = logging.getLogger(__name__)


async def fetch_permission_id_by_slug(admin_client: httpx.AsyncClient, slug: str) -> uuid.UUID | None:
    """Return the Admin catalog id for `slug`, or None if Admin CP is unreachable or the slug is
    absent (callers treat None as fail-closed — never open on an unresolved permission)."""
    try:
        resp = await admin_client.get("/internal/v1/permissions")
        resp.raise_for_status()
        for perm in resp.json():
            if perm.get("slug") == slug:
                return uuid.UUID(str(perm["id"]))
    except Exception:
        log.warning('{"action": "permission_id_unresolved", "slug": "%s"}', slug)
        return None
    return None


async def _require_user(session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> None:
    exists = await session.scalar(
        select(User.id).where(User.id == user_id, User.organization_id == org_id)
    )
    if exists is None:
        raise NotFoundError(f"User {user_id} not found")


async def effective_permission_ids(
    session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID
) -> list[uuid.UUID]:
    """Union of permission ids across the user's roles — one join, no N+1.

    Scoped on `Role.organization_id == org_id` in addition to `user_id` (council review
    defense-in-depth fix): every current write path (set_user_roles validates each role is in-org
    before assigning) already makes a cross-org UserRole unreachable, so this join is not currently
    exercised by any known bug — but without it, a bad migration or a future direct write that
    ever created one would leak another org's permissions straight into `can()`'s effective set, a
    silent cross-org privilege escalation. `user_role_view` below already scopes this same join;
    this brings the two into parity."""
    await _require_user(session, org_id, user_id)
    result = await session.scalars(
        select(RolePermission.permission_id)
        .join(UserRole, UserRole.role_id == RolePermission.role_id)
        .join(Role, Role.id == RolePermission.role_id)
        .where(UserRole.user_id == user_id, Role.organization_id == org_id)
        .distinct()
    )
    return list(result)


async def can(
    session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID, permission_id: uuid.UUID
) -> bool:
    """Membership in the effective set — the seam a future enforcement dependency calls (AD-06)."""
    return permission_id in set(await effective_permission_ids(session, org_id, user_id))


async def user_role_view(
    session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID
) -> list[dict]:
    """The user's roles, each with its composed permission ids — the shape Admin CP renders (§57)."""
    await _require_user(session, org_id, user_id)
    roles = await session.scalars(
        select(Role)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id, Role.organization_id == org_id)
        .order_by(Role.name)
    )
    view: list[dict] = []
    for role in roles:
        perm_ids = await session.scalars(
            select(RolePermission.permission_id).where(RolePermission.role_id == role.id)
        )
        view.append({"role_id": role.id, "name": role.name, "permission_ids": list(perm_ids)})
    return view
