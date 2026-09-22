"""Usage HTTP routes — org-scoped read-through proxy to billing's internal usage API
(add-org-cp-usage-proxy). Thin handlers; all logic in service. Service bearer + tenant context
on every route, reused verbatim (design D2) — no new auth mechanism."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import httpx
from dependencies import get_billing_client, require_org_context, require_service_key
from fastapi import APIRouter, Depends

from . import service

router = APIRouter(prefix="/v1/usage", tags=["usage"], dependencies=[Depends(require_service_key)])


@router.get("/summary")
async def usage_summary(
    start: date | None = None,
    end: date | None = None,
    org_id: uuid.UUID = Depends(require_org_context),
    billing_client: httpx.AsyncClient = Depends(get_billing_client),
) -> Any:
    return await service.get_usage_summary(billing_client, str(org_id), start, end)


@router.get("/timeseries")
async def usage_timeseries(
    start: date | None = None,
    end: date | None = None,
    org_id: uuid.UUID = Depends(require_org_context),
    billing_client: httpx.AsyncClient = Depends(get_billing_client),
) -> Any:
    return await service.get_usage_timeseries(billing_client, str(org_id), start, end)


@router.get("/by-user")
async def usage_by_user(
    start: date | None = None,
    end: date | None = None,
    org_id: uuid.UUID = Depends(require_org_context),
    billing_client: httpx.AsyncClient = Depends(get_billing_client),
) -> Any:
    return await service.get_usage_by_user(billing_client, str(org_id), start, end)


@router.get("/by-key")
async def usage_by_key(
    start: date | None = None,
    end: date | None = None,
    org_id: uuid.UUID = Depends(require_org_context),
    billing_client: httpx.AsyncClient = Depends(get_billing_client),
) -> Any:
    return await service.get_usage_by_key(billing_client, str(org_id), start, end)
