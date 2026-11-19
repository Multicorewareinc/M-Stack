"""Organizations HTTP routes. Thin: parse, delegate to service, return. Auth enforced at
router level (require_admin_key on the public /v1 surface; require_service_key on the internal
Org CP-facing surface — distinct bearers, see dependencies.py)."""

from __future__ import annotations

import uuid

import httpx
from db import get_session
from dependencies import (
    get_billing_client,
    get_org_client,
    get_sessionmaker,
    require_admin_key,
    require_service_key,
)
from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modules.audit import service as audit
from modules.plans.schemas import PlanOut

from . import service
from .schemas import OrgCreate, OrgOut, OrgUpdate

router = APIRouter(
    prefix="/v1/organizations", tags=["organizations"], dependencies=[Depends(require_admin_key)]
)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=OrgOut)
async def create(
    body: OrgCreate,
    request: Request,
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    org_client: httpx.AsyncClient = Depends(get_org_client),
    billing_client: httpx.AsyncClient = Depends(get_billing_client),
) -> OrgOut:
    # Cross-service workflow (provisioning → active/failed), not a single-commit CRUD (D1).
    org = await service.create_org_workflow(sessionmaker, org_client, billing_client, body)
    await audit.record_detached(
        sessionmaker,
        organization_id=org.id,
        action="organization.created",
        resource_type="organization",
        resource_id=org.id,
        resource_name=org.name,
        details={"plan_id": str(org.plan_id)},
        **audit.ctx(request),
    )
    return service.to_org_out(org)


@router.get("", response_model=list[OrgOut])
async def list_all(
    search: str = "",
    session: AsyncSession = Depends(get_session, scope="function"),
    org_client: httpx.AsyncClient = Depends(get_org_client),
) -> list[OrgOut]:
    return await service.list_orgs(session, org_client, search)


@router.get("/{org_id}", response_model=OrgOut)
async def get_one(
    org_id: uuid.UUID,
    session: AsyncSession = Depends(get_session, scope="function"),
    org_client: httpx.AsyncClient = Depends(get_org_client),
) -> OrgOut:
    return await service.get_org_detail(session, org_client, org_id)


@router.patch("/{org_id}", response_model=OrgOut)
async def patch(
    org_id: uuid.UUID,
    body: OrgUpdate,
    request: Request,
    session: AsyncSession = Depends(get_session, scope="function"),
    billing_client: httpx.AsyncClient = Depends(get_billing_client),
) -> OrgOut:
    before = await service.get_org(session, org_id)
    old_plan_id, old_name, old_status = before.plan_id, before.name, before.status
    org = await service.update_org(session, billing_client, org_id, body)
    common = {
        "organization_id": org_id,
        "resource_type": "organization",
        "resource_id": org_id,
        "resource_name": org.name,
        **audit.ctx(request),
    }
    if org.plan_id != old_plan_id:
        await audit.record(
            session,
            action="organization.plan_changed",
            details={"from_plan_id": str(old_plan_id), "to_plan_id": str(org.plan_id)},
            **common,
        )
    if org.status != old_status:
        await audit.record(
            session,
            action="organization.status_changed",
            details={"from": old_status, "to": org.status},
            **common,
        )
    if org.name != old_name:
        await audit.record(
            session,
            action="organization.renamed",
            details={"from": old_name, "to": org.name},
            **common,
        )
    return service.to_org_out(org)


@router.delete("/{org_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(
    org_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session, scope="function"),
) -> None:
    name = (await service.get_org(session, org_id)).name  # 404s here exactly as delete would
    await service.delete_org(session, org_id)
    await audit.record(
        session,
        organization_id=org_id,
        action="organization.deleted",
        resource_type="organization",
        resource_id=org_id,
        resource_name=name,
        **audit.ctx(request),
    )


@router.post("/{org_id}/retry", response_model=OrgOut)
async def retry(
    org_id: uuid.UUID,
    request: Request,
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    org_client: httpx.AsyncClient = Depends(get_org_client),
    billing_client: httpx.AsyncClient = Depends(get_billing_client),
) -> OrgOut:
    org = await service.retry_org_workflow(sessionmaker, org_client, billing_client, org_id)
    await audit.record_detached(
        sessionmaker,
        organization_id=org_id,
        action="organization.provisioning_retried",
        resource_type="organization",
        resource_id=org_id,
        resource_name=org.name,
        **audit.ctx(request),
    )
    return service.to_org_out(org)


# Internal read-only surface — Org CP resolves an org's plan for its organization-summary proxy.
# Gated by require_service_key (NOT require_admin_key): a distinct bearer from the super-admin
# credential, so Org CP never needs to hold the super-admin key to call this.
internal_router = APIRouter(
    prefix="/internal/v1/organizations",
    tags=["organizations-internal"],
    dependencies=[Depends(require_service_key)],
)


@internal_router.get("/{org_id}/plan", response_model=PlanOut)
async def internal_get_plan(
    org_id: uuid.UUID, session: AsyncSession = Depends(get_session, scope="function")
) -> PlanOut:
    return await service.get_org_plan(session, org_id)
