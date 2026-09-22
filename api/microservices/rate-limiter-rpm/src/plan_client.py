"""Best-effort lookup of an org's plan RPM limit from admin-control-plane (AD-04: rate
limiters enforce the resolving org's plan, not one static number for everyone).

`/check` is on the gateway's synchronous request path, so this never blocks on a fresh HTTP
round trip per request: results are cached briefly per org, and any failure (admin-control-plane
unreachable, org has no plan, timeout) degrades to None so the caller falls back to the static
RATE_LIMITS config (fail open) instead of erroring the request.
"""

from __future__ import annotations

import time

import httpx


class PlanClient:
    def __init__(self, base_url: str, service_api_key: str, *, ttl: int = 30) -> None:
        self._enabled = bool(base_url)
        self._client = httpx.AsyncClient(base_url=base_url, timeout=2.0) if self._enabled else None
        self._headers = {"Authorization": f"Bearer {service_api_key}"}
        self._ttl = ttl
        self._cache: dict[str, tuple[float, int | None]] = {}

    async def get_rpm_limit(self, org_id: str) -> int | None:
        """The org's plan `rpm` (0 = plan says unlimited), or None if unknown/unreachable."""
        if not self._enabled or not org_id:
            return None
        now = time.monotonic()
        cached = self._cache.get(org_id)
        if cached is not None and cached[0] > now:
            return cached[1]
        try:
            resp = await self._client.get(f"/internal/v1/organizations/{org_id}/plan", headers=self._headers)
            resp.raise_for_status()
            value: int | None = int(resp.json()["rpm"])
        except Exception:
            value = None
        self._cache[org_id] = (now + self._ttl, value)
        return value

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
