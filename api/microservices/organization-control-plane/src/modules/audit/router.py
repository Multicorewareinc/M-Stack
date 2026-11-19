"""Audit-log read routes.

- `GET /api/audit-logs` — end-user JWT surface (like /api/api-keys): the org is the caller's own,
  taken from the verified token, never a client value — tenant isolation. Any signed-in org member
  may read their own organization's log.
- `GET /internal/v1/audit-logs` — service-bearer surface for Admin CP's cross-org view; optional
  `organization_id` narrows it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from db import get_session
from dependencies import require_service_key
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from modules.auth.dependencies import get_current_user
from modules.users.models import User

from . import service
from .schemas import AuditLogPage

router = APIRouter(prefix="/api/audit-logs", tags=["audit-logs"])


@router.get("", response_model=AuditLogPage)
async def list_own_org(
    action: str | None = None,
    resource_type: str | None = None,
    search: str | None = Query(default=None, max_length=200),
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> AuditLogPage:
    rows, total = await service.list_logs(
        session,
        organization_id=user.organization_id,
        action=action,
        resource_type=resource_type,
        search=search,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )
    return AuditLogPage(items=rows, total=total, limit=limit, offset=offset)


internal_router = APIRouter(
    prefix="/internal/v1/audit-logs",
    tags=["audit-logs-internal"],
    dependencies=[Depends(require_service_key)],
)


@internal_router.get("", response_model=AuditLogPage)
async def list_all_orgs(
    organization_id: uuid.UUID | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    search: str | None = Query(default=None, max_length=200),
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> AuditLogPage:
    rows, total = await service.list_logs(
        session,
        organization_id=organization_id,
        action=action,
        resource_type=resource_type,
        search=search,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )
    return AuditLogPage(items=rows, total=total, limit=limit, offset=offset)
