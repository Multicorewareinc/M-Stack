"""Organization user directory — a synchronous read-through proxy to Org CP's internal user
endpoints (AD-02, ADR-018). No local projection table (AD-05). The proxy forwards Org CP's JSON
verbatim (Org CP owns the user shape, ADR-019/§18) and maps errors to a clean typed envelope:
Org CP 404 → 404, transport failure / 5xx → 502 — the Admin UI never sees Org PG internals (§42)."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from errors import NotFoundError, UpstreamError
from modules.permissions import service as permissions_service


async def _proxy_get(client: httpx.AsyncClient, path: str) -> Any:
    try:
        resp = await client.get(path)
    except httpx.HTTPError as exc:
        raise UpstreamError("Org CP unavailable") from exc
    if resp.status_code == 404:
        raise NotFoundError("Not found")
    if resp.status_code >= 500 or resp.status_code != 200:
        raise UpstreamError(f"Org CP returned {resp.status_code}")
    return resp.json()


async def list_org_users(client: httpx.AsyncClient, org_id: uuid.UUID) -> Any:
    return await _proxy_get(client, f"/internal/v1/organizations/{org_id}/users")


async def get_org_user(client: httpx.AsyncClient, org_id: uuid.UUID, user_id: uuid.UUID) -> Any:
    return await _proxy_get(client, f"/internal/v1/organizations/{org_id}/users/{user_id}")


async def get_org_user_roles(
    client: httpx.AsyncClient, org_id: uuid.UUID, user_id: uuid.UUID
) -> Any:
    return await _proxy_get(client, f"/internal/v1/organizations/{org_id}/users/{user_id}/roles")


async def list_platform_users(org_client: httpx.AsyncClient) -> Any:
    """Platform-wide, unscoped by org (Super-Admin only) — Org CP owns a matching unscoped
    internal surface for exactly this proxy (§45 tenant-scoping is about /v1 tenant operations,
    not this cross-tenant admin view)."""
    return await _proxy_get(org_client, "/internal/v1/users")


async def get_platform_user_detail(
    session: AsyncSession, org_client: httpx.AsyncClient, user_id: uuid.UUID
) -> Any:
    """Composes the flat user-detail view the Admin Portal renders: the user record (org learned
    from it, not known ahead of time — one Org CP call), its composed roles (name + permission
    ids — a second Org CP call), and the master permission catalog to resolve ids to slugs (a
    LOCAL query — Admin CP owns this table directly, no third network call, ADR-019)."""
    user = await _proxy_get(org_client, f"/internal/v1/users/{user_id}")
    org_id = user["organization_id"]
    role_views = await _proxy_get(org_client, f"/internal/v1/organizations/{org_id}/users/{user_id}/roles")

    catalog = await permissions_service.list_permissions(session)
    slug_by_id = {str(p.id): p.slug for p in catalog}
    permission_ids: set[str] = set()
    for role in role_views:
        permission_ids.update(str(pid) for pid in role["permission_ids"])

    return {
        **user,
        "roles": [role["name"] for role in role_views],
        "effective_permissions": sorted(slug_by_id.get(pid, pid) for pid in permission_ids),
    }
