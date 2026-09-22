"""Platform summary business logic — counts + org status breakdown, backend-derived (never a
per-org fan-out, §184). Active user count is a single cross-service call to Org CP's own
platform-wide count, not a sum of per-org counts (no N+1)."""

from __future__ import annotations

from collections import Counter

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from errors import UpstreamError
from modules.organizations.models import Organization
from modules.plans.models import Plan

from .schemas import PlatformSummary


async def _active_user_count(org_client: httpx.AsyncClient) -> int:
    try:
        resp = await org_client.get("/internal/v1/organizations/active-user-count")
    except httpx.HTTPError as exc:
        raise UpstreamError("Org CP unavailable") from exc
    if resp.status_code != 200:
        raise UpstreamError(f"Org CP returned {resp.status_code} fetching active user count")
    return resp.json()["count"]


async def get_platform_summary(
    session: AsyncSession, org_client: httpx.AsyncClient
) -> PlatformSummary:
    statuses = list(await session.scalars(select(Organization.status)))
    by_status = Counter(statuses)
    plans_count = await session.scalar(select(func.count()).select_from(Plan))
    active_users = await _active_user_count(org_client)

    return PlatformSummary(
        organizations=len(statuses),
        active_users=active_users,
        plans=plans_count or 0,
        provisioning=by_status.get("provisioning", 0),
        active=by_status.get("active", 0),
        failed=by_status.get("failed", 0),
    )
