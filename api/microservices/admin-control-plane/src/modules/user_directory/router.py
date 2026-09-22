"""Admin CP organization user directory — read-through proxy routes to Org CP (AD-02). Thin:
delegate to the proxy service, forward the JSON. Super-admin bearer at router level. The Admin
UI talks only to Admin CP (§42)."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from db import get_session
from dependencies import get_org_client, require_admin_key
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from . import service

router = APIRouter(
    prefix="/v1/organizations", tags=["user-directory"], dependencies=[Depends(require_admin_key)]
)


@router.get("/{organization_id}/users")
async def list_users(
    organization_id: uuid.UUID, org_client: httpx.AsyncClient = Depends(get_org_client)
) -> Any:
    return await service.list_org_users(org_client, organization_id)


@router.get("/{organization_id}/users/{user_id}")
async def user_detail(
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    org_client: httpx.AsyncClient = Depends(get_org_client),
) -> Any:
    return await service.get_org_user(org_client, organization_id, user_id)


@router.get("/{organization_id}/users/{user_id}/roles")
async def user_roles(
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    org_client: httpx.AsyncClient = Depends(get_org_client),
) -> Any:
    return await service.get_org_user_roles(org_client, organization_id, user_id)


# Flat, platform-wide user directory (Super-Admin only) — a separate router (own prefix) since
# it isn't org-scoped like the routes above.
platform_router = APIRouter(
    prefix="/v1/users", tags=["user-directory"], dependencies=[Depends(require_admin_key)]
)


@platform_router.get("")
async def platform_list(org_client: httpx.AsyncClient = Depends(get_org_client)) -> Any:
    return await service.list_platform_users(org_client)


@platform_router.get("/{user_id}")
async def platform_detail(
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    org_client: httpx.AsyncClient = Depends(get_org_client),
) -> Any:
    return await service.get_platform_user_detail(session, org_client, user_id)
