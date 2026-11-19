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
from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from modules.audit import service as audit

from . import service
from .schemas import PermissionCreate, PermissionOut, PermissionUpdate

router = APIRouter(
    prefix="/v1/permissions", tags=["permissions"], dependencies=[Depends(require_admin_key)]
)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=PermissionOut)
async def create(
    body: PermissionCreate,
    request: Request,
    session: AsyncSession = Depends(get_session, scope="function"),
) -> PermissionOut:
    permission = await service.create_permission(session, body)
    await audit.record(
        session,
        action="permission.created",
        resource_type="permission",
        resource_id=permission.id,
        resource_name=permission.slug,
        **audit.ctx(request),
    )
    return permission


@router.get("", response_model=list[PermissionOut])
async def list_all(
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[PermissionOut]:
    return await service.list_permissions(session)


@router.get("/{permission_id}", response_model=PermissionOut)
async def get_one(
    permission_id: uuid.UUID, session: AsyncSession = Depends(get_session, scope="function")
) -> PermissionOut:
    return await service.get_permission(session, permission_id)


@router.patch("/{permission_id}", response_model=PermissionOut)
async def patch(
    permission_id: uuid.UUID,
    body: PermissionUpdate,
    request: Request,
    session: AsyncSession = Depends(get_session, scope="function"),
    org_client: httpx.AsyncClient = Depends(get_org_client),
) -> PermissionOut:
    permission = await service.update_permission(session, org_client, permission_id, body)
    await audit.record(
        session,
        action="permission.updated",
        resource_type="permission",
        resource_id=permission_id,
        resource_name=permission.slug,
        details={"changed": sorted(body.model_dump(exclude_unset=True))},
        **audit.ctx(request),
    )
    return permission


@router.delete("/{permission_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(
    permission_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session, scope="function"),
    org_client: httpx.AsyncClient = Depends(get_org_client),
) -> None:
    slug = (await service.get_permission(session, permission_id)).slug  # 404s as delete would
    await service.delete_permission(session, org_client, permission_id)
    await audit.record(
        session,
        action="permission.deleted",
        resource_type="permission",
        resource_id=permission_id,
        resource_name=slug,
        **audit.ctx(request),
    )


# Internal read-only surface — Org CP validates role_permissions.permission_id against this.
internal_router = APIRouter(
    prefix="/internal/v1/permissions",
    tags=["permissions-internal"],
    dependencies=[Depends(require_service_key)],
)


@internal_router.get("", response_model=list[PermissionOut])
async def internal_list(
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[PermissionOut]:
    return await service.list_permissions(session)


@internal_router.get("/{permission_id}", response_model=PermissionOut)
async def internal_get(
    permission_id: uuid.UUID, session: AsyncSession = Depends(get_session, scope="function")
) -> PermissionOut:
    return await service.get_permission(session, permission_id)
