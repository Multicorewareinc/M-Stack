"""Permission-catalog read-through proxy to Admin CP's internal master list (ADR-018/019) — the
catalog is platform-owned, Org CP never stores or writes it. Mirrors admin-control-plane's
user_directory proxy pattern: forward Admin CP's JSON verbatim, map errors to a clean envelope."""

from __future__ import annotations

from typing import Any

import httpx

from errors import UpstreamError


async def list_permissions(client: httpx.AsyncClient) -> Any:
    try:
        resp = await client.get("/internal/v1/permissions")
    except httpx.HTTPError as exc:
        raise UpstreamError("Admin CP unavailable") from exc
    if resp.status_code != 200:
        raise UpstreamError(f"Admin CP returned {resp.status_code} fetching permissions")
    return resp.json()
