"""Usage read-through proxy to billing's internal API (add-org-cp-usage-proxy) — mirrors
modules/permissions/service.py's proxy-to-Admin-CP shape exactly: forward the upstream JSON
verbatim, map transport/status errors to a clean UpstreamError envelope."""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx

from errors import UpstreamError


def _period_params(org_id: str, start: date | None, end: date | None) -> dict[str, Any]:
    """Build the outbound query params, omitting start/end entirely when absent (design D5) —
    httpx's `params=` serializes a None value to an empty string (`start=`) rather than dropping
    the key, which would make billing's own `date | None` param parsing 422 on an empty string
    and silently break billing's default-to-current-month behavior. Never include a None-valued
    key."""
    params: dict[str, Any] = {"org_id": org_id}
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    return params


async def get_usage_summary(
    billing_client: httpx.AsyncClient, org_id: str, start: date | None, end: date | None
) -> Any:
    try:
        resp = await billing_client.get(
            "/internal/v1/usage/summary", params=_period_params(org_id, start, end)
        )
    except httpx.HTTPError as exc:
        raise UpstreamError("Billing unavailable") from exc
    if resp.status_code != 200:
        raise UpstreamError(f"Billing returned {resp.status_code} fetching usage summary")
    return resp.json()


async def get_usage_timeseries(
    billing_client: httpx.AsyncClient, org_id: str, start: date | None, end: date | None
) -> Any:
    try:
        resp = await billing_client.get(
            "/internal/v1/usage/timeseries", params=_period_params(org_id, start, end)
        )
    except httpx.HTTPError as exc:
        raise UpstreamError("Billing unavailable") from exc
    if resp.status_code != 200:
        raise UpstreamError(f"Billing returned {resp.status_code} fetching usage timeseries")
    return resp.json()


async def get_usage_by_user(
    billing_client: httpx.AsyncClient, org_id: str, start: date | None, end: date | None
) -> Any:
    try:
        resp = await billing_client.get(
            "/internal/v1/usage/by-user", params=_period_params(org_id, start, end)
        )
    except httpx.HTTPError as exc:
        raise UpstreamError("Billing unavailable") from exc
    if resp.status_code != 200:
        raise UpstreamError(f"Billing returned {resp.status_code} fetching usage by user")
    return resp.json()


async def get_usage_by_key(
    billing_client: httpx.AsyncClient, org_id: str, start: date | None, end: date | None
) -> Any:
    try:
        resp = await billing_client.get(
            "/internal/v1/usage/by-key", params=_period_params(org_id, start, end)
        )
    except httpx.HTTPError as exc:
        raise UpstreamError("Billing unavailable") from exc
    if resp.status_code != 200:
        raise UpstreamError(f"Billing returned {resp.status_code} fetching usage by key")
    return resp.json()
