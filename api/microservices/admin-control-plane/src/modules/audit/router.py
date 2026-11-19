"""Audit-log read route for the Admin Portal: every organization's events plus the platform's own.
Gated like the other /v1 routes (super-admin JWT or the admin key)."""

from __future__ import annotations

import uuid
from datetime import datetime

import httpx
from db import get_session
from dependencies import get_org_client, require_admin_key
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from errors import UnprocessableError

from . import service
from .schemas import AuditLogPage

router = APIRouter(
    prefix="/v1/audit-logs", tags=["audit-logs"], dependencies=[Depends(require_admin_key)]
)


@router.get("", response_model=AuditLogPage)
async def list_all(
    organization_id: uuid.UUID | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    search: str | None = Query(default=None, max_length=200),
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session, scope="function"),
    org_client: httpx.AsyncClient = Depends(get_org_client),
) -> AuditLogPage:
    if offset + limit > service.MAX_WINDOW:
        raise UnprocessableError(
            f"Cannot page beyond the newest {service.MAX_WINDOW} events; narrow the filters instead."
        )
    return await service.list_logs(
        session,
        org_client,
        organization_id=organization_id,
        action=action,
        resource_type=resource_type,
        search=search,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )
