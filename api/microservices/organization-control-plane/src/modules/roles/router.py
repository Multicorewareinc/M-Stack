"""Roles HTTP routes — tenant-scoped role CRUD, permission composition (validated against Admin
CP), and user-role assignment. Thin handlers; all logic in service. Service bearer + tenant
context on every route."""

from __future__ import annotations

import uuid

import httpx
from db import get_session
from dependencies import get_admin_client, require_org_context, require_service_key
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from . import service
from .schemas import PermissionSet, PermissionUsage, RoleCreate, RoleOut, RoleSet, RoleUpdate

router = APIRouter(
    prefix="/v1", tags=["roles"], dependencies=[Depends(require_service_key)]
)


# ---- Roles -------------------------------------------------------------------------------

@router.post("/roles", status_code=status.HTTP_201_CREATED, response_model=RoleOut)
async def create_role(
    body: RoleCreate,
    org_id: uuid.UUID = Depends(require_org_context),
    session: AsyncSession = Depends(get_session),
) -> RoleOut:
    return await service.create_role(session, org_id, body)


@router.get("/roles", response_model=list[RoleOut])
async def list_roles(
    org_id: uuid.UUID = Depends(require_org_context),
    session: AsyncSession = Depends(get_session),
) -> list[RoleOut]:
    return await service.list_roles(session, org_id)


@router.get("/roles/{role_id}", response_model=RoleOut)
async def get_role(
    role_id: uuid.UUID,
    org_id: uuid.UUID = Depends(require_org_context),
    session: AsyncSession = Depends(get_session),
) -> RoleOut:
    return await service.get_role(session, org_id, role_id)


@router.patch("/roles/{role_id}", response_model=RoleOut)
async def patch_role(
    role_id: uuid.UUID,
    body: RoleUpdate,
    org_id: uuid.UUID = Depends(require_org_context),
    session: AsyncSession = Depends(get_session),
) -> RoleOut:
    return await service.update_role(session, org_id, role_id, body)


@router.delete("/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(
    role_id: uuid.UUID,
    org_id: uuid.UUID = Depends(require_org_context),
    session: AsyncSession = Depends(get_session),
) -> None:
    await service.delete_role(session, org_id, role_id)


# ---- Role-permission composition ---------------------------------------------------------

@router.put("/roles/{role_id}/permissions", response_model=PermissionSet)
async def set_role_permissions(
    role_id: uuid.UUID,
    body: PermissionSet,
    org_id: uuid.UUID = Depends(require_org_context),
    session: AsyncSession = Depends(get_session),
    admin_client: httpx.AsyncClient = Depends(get_admin_client),
) -> PermissionSet:
    await service.set_role_permissions(session, admin_client, org_id, role_id, body.permission_ids)
    ids = await service.get_role_permission_ids(session, org_id, role_id)
    return PermissionSet(permission_ids=ids)


@router.get("/roles/{role_id}/permissions", response_model=PermissionSet)
async def get_role_permissions(
    role_id: uuid.UUID,
    org_id: uuid.UUID = Depends(require_org_context),
    session: AsyncSession = Depends(get_session),
) -> PermissionSet:
    ids = await service.get_role_permission_ids(session, org_id, role_id)
    return PermissionSet(permission_ids=ids)


# ---- User-role assignment ----------------------------------------------------------------

@router.put("/users/{user_id}/roles", response_model=RoleSet)
async def set_user_roles(
    user_id: uuid.UUID,
    body: RoleSet,
    org_id: uuid.UUID = Depends(require_org_context),
    session: AsyncSession = Depends(get_session),
) -> RoleSet:
    await service.set_user_roles(session, org_id, user_id, body.role_ids)
    ids = await service.get_user_role_ids(session, org_id, user_id)
    return RoleSet(role_ids=ids)


@router.get("/users/{user_id}/roles", response_model=RoleSet)
async def get_user_roles(
    user_id: uuid.UUID,
    org_id: uuid.UUID = Depends(require_org_context),
    session: AsyncSession = Depends(get_session),
) -> RoleSet:
    ids = await service.get_user_role_ids(session, org_id, user_id)
    return RoleSet(role_ids=ids)


# ---- Internal read surface for Admin CP (ADR-019 reverse-reference check) -----------------
# Platform-wide, NOT org-scoped (mirrors users/router.py's internal_router pattern) — Admin CP
# calls this before deleting a master-list permission to avoid orphaning an org's composition.
internal_router = APIRouter(
    prefix="/internal/v1/roles", tags=["roles-internal"], dependencies=[Depends(require_service_key)]
)


@internal_router.get("/permission-usage", response_model=PermissionUsage)
async def internal_permission_usage(
    permission_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> PermissionUsage:
    return PermissionUsage(in_use=await service.permission_in_use(session, permission_id))
