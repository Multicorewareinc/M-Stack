"""Super-admin invitation route. Gated like user_provisioning: by the super-admin JWT identity
(get_current_user, which also enforces the forced-rotation gate). Thin: delegates to the proxy service.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from dependencies import get_billing_client, get_org_client, get_sessionmaker
from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import async_sessionmaker

from modules.audit import service as audit
from modules.auth.dependencies import get_current_user
from modules.auth.models import AdminUser
from modules.organizations.schemas import OrgCreate
from modules.user_provisioning.service import actor_headers

from . import service
from .schemas import InvitationRequest

router = APIRouter(
    prefix="/v1/invitations",
    tags=["admin-invitations"],
    dependencies=[Depends(get_current_user)],
)


@router.post("", status_code=status.HTTP_201_CREATED)
async def invite(
    body: InvitationRequest,
    request: Request,
    admin: AdminUser = Depends(get_current_user),
    sessionmaker: async_sessionmaker = Depends(get_sessionmaker),
    org_client: httpx.AsyncClient = Depends(get_org_client),
    billing_client: httpx.AsyncClient = Depends(get_billing_client),
) -> Any:
    actor = audit.Actor("admin", str(admin.id), admin.email)
    headers = actor_headers(actor)
    payload = {"email": body.email, "name": body.name, "role_name": body.role}

    if body.new_organization is not None:
        org, invitation = await service.create_with_new_org(
            sessionmaker,
            org_client,
            billing_client,
            OrgCreate(name=body.new_organization.name, plan_id=body.new_organization.plan_id),
            payload,
            headers,
        )
        await audit.record_detached(
            sessionmaker,
            organization_id=org.id,
            action="organization.created",
            resource_type="organization",
            resource_id=str(org.id),
            resource_name=org.name,
            details={"plan_id": str(org.plan_id), "via": "invitation"},
            **audit.ctx(request, actor),
        )
        return {**invitation, "organization_created": True}

    invitation = await service.create_for_existing_org(
        sessionmaker, org_client, body.organization_id, payload, headers
    )
    return {**invitation, "organization_created": False}


@router.get("")
async def list_pending(
    organization_id: uuid.UUID | None = None,
    search: str | None = Query(default=None, max_length=200),
    role: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=25, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    org_client: httpx.AsyncClient = Depends(get_org_client),
) -> Any:
    params = {
        k: str(v)
        for k, v in {
            "organization_id": organization_id,
            "search": search,
            "role": role,
            "limit": limit,
            "offset": offset,
        }.items()
        if v is not None
    }
    return await service.list_invitations(org_client, params)


@router.delete("/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke(
    invitation_id: uuid.UUID,
    admin: AdminUser = Depends(get_current_user),
    org_client: httpx.AsyncClient = Depends(get_org_client),
) -> Response:
    actor = audit.Actor("admin", str(admin.id), admin.email)
    await service.revoke_invitation(org_client, invitation_id, actor_headers(actor))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
