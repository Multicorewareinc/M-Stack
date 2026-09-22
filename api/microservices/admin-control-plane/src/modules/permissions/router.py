"""Master permission list HTTP routes. Thin: parse, delegate, return. Two routers:
- public `/v1/permissions` — Super-Admin CRUD, gated by require_admin_key (super-admin bearer);
- internal `/internal/v1/permissions` — read-only, for Org CP to validate permission ids, gated by
  require_service_key (a bearer DISTINCT from require_admin_key — Org CP never needs the
  super-admin credential to call this). The internal router never writes the catalog (ADR-019)."""

from __future__ import annotations

import uuid

import httpx
from db import get_session
from dependencies import get_org_client, require_admin_key, require_service_key
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from . import service
from .schemas import PermissionCreate, PermissionOut, PermissionUpdate

router = APIRouter(
    prefix="/v1/permissions", tags=["permissions"], dependencies=[Depends(require_admin_key)]
)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=PermissionOut)
async def create(
    body: PermissionCreate, session: AsyncSession = Depends(get_session)
) -> PermissionOut:
    return await service.create_permission(session, body)


@router.get("", response_model=list[PermissionOut])
async def list_all(session: AsyncSession = Depends(get_session)) -> list[PermissionOut]:
    return await service.list_permissions(session)


@router.get("/{permission_id}", response_model=PermissionOut)
async def get_one(
    permission_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> PermissionOut:
    return await service.get_permission(session, permission_id)


@router.patch("/{permission_id}", response_model=PermissionOut)
async def patch(
    permission_id: uuid.UUID,
    body: PermissionUpdate,
    session: AsyncSession = Depends(get_session),
    org_client: httpx.AsyncClient = Depends(get_org_client),
) -> PermissionOut:
    return await service.update_permission(session, org_client, permission_id, body)


@router.delete("/{permission_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(
    permission_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    org_client: httpx.AsyncClient = Depends(get_org_client),
) -> None:
    await service.delete_permission(session, org_client, permission_id)


# Internal read-only surface — Org CP validates role_permissions.permission_id against this.
internal_router = APIRouter(
    prefix="/internal/v1/permissions",
    tags=["permissions-internal"],
    dependencies=[Depends(require_service_key)],
)


@internal_router.get("", response_model=list[PermissionOut])
async def internal_list(session: AsyncSession = Depends(get_session)) -> list[PermissionOut]:
    return await service.list_permissions(session)


@internal_router.get("/{permission_id}", response_model=PermissionOut)
async def internal_get(
    permission_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> PermissionOut:
    return await service.get_permission(session, permission_id)
