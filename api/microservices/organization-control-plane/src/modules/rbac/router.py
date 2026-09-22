"""RBAC resolution routes — effective permissions for a user (tenant-scoped) and the composed
role/permission view for the Admin proxy (internal). Thin handlers over the resolver."""

from __future__ import annotations

import uuid

from db import get_session
from dependencies import require_org_context, require_service_key
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from . import service
from .schemas import PermissionIds, RoleWithPermissions

router = APIRouter(prefix="/v1", tags=["rbac"], dependencies=[Depends(require_service_key)])


@router.get("/users/{user_id}/permissions", response_model=PermissionIds)
async def effective_permissions(
    user_id: uuid.UUID,
    org_id: uuid.UUID = Depends(require_org_context),
    session: AsyncSession = Depends(get_session),
) -> PermissionIds:
    ids = await service.effective_permission_ids(session, org_id, user_id)
    return PermissionIds(permission_ids=ids)


internal_router = APIRouter(
    prefix="/internal/v1/organizations",
    tags=["rbac-internal"],
    dependencies=[Depends(require_service_key)],
)


@internal_router.get(
    "/{organization_id}/users/{user_id}/roles", response_model=list[RoleWithPermissions]
)
async def composed_user_roles(
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> list[RoleWithPermissions]:
    return await service.user_role_view(session, organization_id, user_id)
