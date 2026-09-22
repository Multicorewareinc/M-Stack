"""Platform summary HTTP route. Thin: parse, delegate, return. Auth enforced at router level
(require_admin_key)."""

from __future__ import annotations

import httpx
from db import get_session
from dependencies import get_org_client, require_admin_key
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from . import service
from .schemas import PlatformSummary

router = APIRouter(
    prefix="/v1/platform-summary", tags=["platform-summary"], dependencies=[Depends(require_admin_key)]
)


@router.get("", response_model=PlatformSummary)
async def get_summary(
    session: AsyncSession = Depends(get_session),
    org_client: httpx.AsyncClient = Depends(get_org_client),
) -> PlatformSummary:
    return await service.get_platform_summary(session, org_client)
