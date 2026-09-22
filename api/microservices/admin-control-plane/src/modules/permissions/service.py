"""Master permission list business logic. All DB access here; router is thin. Services flush()
but never commit() — get_session owns the transaction (design D3). `slug` uniqueness is enforced
by the DB constraint (race-free) and surfaced as a clean 409, mirroring the plans module."""

from __future__ import annotations

import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from errors import ConflictError, NotFoundError, UpstreamError

from .models import Permission
from .schemas import PermissionCreate, PermissionUpdate, slug_for


async def _flush_unique(session: AsyncSession, slug: str) -> None:
    """Flush, converting the DB UNIQUE(slug) violation into a clean 409 — race-free (the DB
    constraint is the source of truth, not a pre-check SELECT). get_session owns rollback (D3)."""
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError(f"Permission '{slug}' already exists", field="action") from exc


async def create_permission(session: AsyncSession, data: PermissionCreate) -> Permission:
    slug = slug_for(data.resource, data.action)
    perm = Permission(
        resource=data.resource, action=data.action, slug=slug, description=data.description
    )
    session.add(perm)
    await _flush_unique(session, slug)
    return perm


async def list_permissions(session: AsyncSession) -> list[Permission]:
    # ponytail: unpaginated; the master catalog is small and Super-Admin curated.
    result = await session.scalars(select(Permission).order_by(Permission.slug))
    return list(result)


async def get_permission(session: AsyncSession, permission_id: uuid.UUID) -> Permission:
    perm = await session.get(Permission, permission_id)
    if perm is None:
        raise NotFoundError(f"Permission {permission_id} not found")
    return perm


async def update_permission(
    session: AsyncSession, org_client: httpx.AsyncClient, permission_id: uuid.UUID, data: PermissionUpdate
) -> Permission:
    """Update (including soft-deactivate via `is_active:false`) a master-list permission.

    Deactivating a permission still referenced by an org's role composition is blocked (409) —
    this is the primary path the Admin portal actually uses (it soft-deactivates, never hard-deletes,
    per the admin-permissions spec); `delete_permission` below carries the identical guard for the
    hard-delete surface some callers may still use directly."""
    perm = await get_permission(session, permission_id)
    fields = data.model_dump(exclude_unset=True)
    deactivating = fields.get("is_active") is False and perm.is_active
    if deactivating:
        await _reject_if_referenced(org_client, permission_id)
    for key, value in fields.items():
        setattr(perm, key, value)
    # Recompute slug if resource/action changed.
    if "resource" in fields or "action" in fields:
        perm.slug = slug_for(perm.resource, perm.action)
    await _flush_unique(session, perm.slug)
    return perm


async def _reject_if_referenced(org_client: httpx.AsyncClient, permission_id: uuid.UUID) -> None:
    """Shared reverse-reference check (council review fix) — 409 if any org's role still composes
    this permission id; fails CLOSED (502) if Org CP can't be asked."""
    try:
        resp = await org_client.get(
            "/internal/v1/roles/permission-usage", params={"permission_id": str(permission_id)}
        )
    except httpx.HTTPError as exc:
        raise UpstreamError("Org CP permission-usage check unavailable") from exc
    if resp.status_code != 200:
        raise UpstreamError(f"Org CP returned {resp.status_code} checking permission usage")
    if resp.json().get("in_use"):
        raise ConflictError(
            f"Permission {permission_id} is composed into at least one organization's role"
        )


async def delete_permission(
    session: AsyncSession, org_client: httpx.AsyncClient, permission_id: uuid.UUID
) -> None:
    """Hard-delete a master-list permission — blocked (409) if any org's role composition still
    references it (council review fix: this used to have no reverse-check at all, silently
    orphaning role_permissions rows on the Org side). The Admin portal itself never calls this
    (it soft-deactivates via PATCH is_active:false, see update_permission above); this endpoint
    exists for direct/API callers, so it carries the identical guard rather than relying on the UI
    never exercising it. Fail CLOSED (502) if Org CP is unreachable, since we cannot confirm
    deletion is safe."""
    perm = await get_permission(session, permission_id)
    await _reject_if_referenced(org_client, permission_id)
    await session.delete(perm)
    await session.flush()
