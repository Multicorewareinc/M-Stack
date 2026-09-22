"""Organization tenant HTTP routes. `internal_router` is Admin CP-only (org-creation workflow).
`router` is the tenant-scoped public surface (org summary). Thin: parse, delegate, return. Init
returns 201 (created) or 200 (idempotent re-init) so the caller can tell the two apart (D2)."""

from __future__ import annotations

import uuid

import httpx
from db import get_session
from dependencies import get_admin_client, require_org_context, require_service_key
from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from . import service
from .schemas import OrgInit, OrgOut, OrgSummary

internal_router = APIRouter(
    prefix="/internal/v1/organizations",
    tags=["organizations-internal"],
    dependencies=[Depends(require_service_key)],
)


@internal_router.post("", status_code=status.HTTP_201_CREATED, response_model=OrgOut)
async def init(
    body: OrgInit,
    response: Response,
    session: AsyncSession = Depends(get_session),
    admin_client: httpx.AsyncClient = Depends(get_admin_client),
) -> OrgOut:
    org, created = await service.init_org(session, body)
    # Seed default org roles in the SAME transaction (atomic with the tenant row) so provisioning
    # is all-or-nothing (ADR-029/AD-04). Idempotent, so a re-init backfills a role-less org.
    await service.seed_default_roles(session, admin_client, body.id)
    if not created:
        response.status_code = status.HTTP_200_OK  # idempotent re-init
    return org


@internal_router.get("/{organization_id}", response_model=OrgOut)
async def get_one(
    organization_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> OrgOut:
    return await service.get_org(session, organization_id)


router = APIRouter(
    prefix="/v1/organization", tags=["organization"], dependencies=[Depends(require_service_key)]
)


@router.get("/summary", response_model=OrgSummary)
async def get_summary(
    org_id: uuid.UUID = Depends(require_org_context),
    session: AsyncSession = Depends(get_session),
    admin_client: httpx.AsyncClient = Depends(get_admin_client),
) -> OrgSummary:
    return await service.get_org_summary(session, admin_client, org_id)
