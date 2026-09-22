"""Super-admin user provisioning routes (ADR-029/AD-02b,AD-02c). Gated by the super-admin JWT
identity (get_current_user — every admin_users row is a super-admin, and the dependency also enforces
the forced-rotation gate), NOT the fixed require_admin_key seam the other /v1 routes use (AD-07).
Thin: delegate to the proxy service, which talks to the Org CP."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from dependencies import get_billing_client, get_org_client, get_sessionmaker
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import async_sessionmaker

from modules.auth.dependencies import get_current_user
from modules.organizations.schemas import OrgCreate

from . import service
from .schemas import OrgWithOwnerCreate, OwnerCreate

router = APIRouter(
    prefix="/v1/organizations",
    tags=["admin-user-provisioning"],
    dependencies=[Depends(get_current_user)],  # super-admin JWT + forced-rotation gate
)


@router.post("/with-owner", status_code=status.HTTP_201_CREATED)
async def create_org_with_owner(
    body: OrgWithOwnerCreate,
    sessionmaker: async_sessionmaker = Depends(get_sessionmaker),
    org_client: httpx.AsyncClient = Depends(get_org_client),
    billing_client: httpx.AsyncClient = Depends(get_billing_client),
) -> Any:
    return await service.create_org_with_owner(
        sessionmaker,
        org_client,
        billing_client,
        OrgCreate(name=body.name, plan_id=body.plan_id),
        body.owner.model_dump(),
    )


@router.post("/{org_id}/users", status_code=status.HTTP_201_CREATED)
async def create_user_for_org(
    org_id: uuid.UUID,
    body: OwnerCreate,
    sessionmaker: async_sessionmaker = Depends(get_sessionmaker),
    org_client: httpx.AsyncClient = Depends(get_org_client),
) -> Any:
    return await service.create_user_for_org(sessionmaker, org_client, org_id, body.model_dump())
