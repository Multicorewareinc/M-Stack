"""Permission-catalog HTTP route — read-only proxy (Org CP has no permissions of its own; the
catalog is Admin CP's master list). Not tenant-scoped: the catalog isn't tenant data, so no
require_org_context here — auth is just the service bearer at router level."""

from __future__ import annotations

from typing import Any

import httpx
from dependencies import get_admin_client, require_service_key
from fastapi import APIRouter, Depends

from . import service

router = APIRouter(
    prefix="/v1/permissions", tags=["permissions"], dependencies=[Depends(require_service_key)]
)


@router.get("")
async def list_all(admin_client: httpx.AsyncClient = Depends(get_admin_client)) -> Any:
    return await service.list_permissions(admin_client)
