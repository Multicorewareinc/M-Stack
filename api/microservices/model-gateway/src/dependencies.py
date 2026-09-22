"""Request dependencies, run before any /v1 route body.

- verify_api_key: per-org API-key verification (ADR-026) — hashes the bearer, resolves it via a
  Redis read-through cache backed by the Org CP verify endpoint, re-validates on every request, and
  exposes the resolved org_id as request.state.principal (rejects before the upstream is contacted).
- enforce_policies: generic pluggable policy chain (docs/policy-plane.md). Inert
  when POLICY_ENDPOINTS is empty. NOTE: httpx is used here (verify + policy) outside service.py —
  the enforcement/verify seam, distinct from upstream forwarding (ADR-004).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime

import httpx
from fastapi import Request
from keygen import hash_key
from redis.exceptions import RedisError

from errors import PolicyDeniedError, ServiceUnavailableError, UnauthorizedError

log = logging.getLogger(__name__)

# One opaque, constant-shape rejection for EVERY authentication failure (unknown / revoked / expired /
# owner-inactive / malformed) — never enumerate which check failed (ADR-026, reference §12.1).
_INVALID = "Invalid API key"


def _cache_key(key_hash: str) -> str:
    return f"apikey:{key_hash}"


def _parse_ts(value) -> datetime | None:
    """Parse an ISO timestamp (from Org CP JSON or the cached JSON) to an aware datetime; normalize a
    naive value to UTC (portability, matches Org CP compute_status)."""
    if value is None:
        return None
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def _cache_get(request: Request, key_hash: str) -> dict | None:
    """Read-through cache read. A Valkey fault degrades to a miss (None), never a 5xx."""
    try:
        raw = await request.app.state.redis.get(_cache_key(key_hash))
    except RedisError:
        log.warning('{"action": "apikey.cache_get_degraded", "reason": "redis_unavailable"}')
        return None
    return json.loads(raw) if raw else None


async def _cache_put(request: Request, key_hash: str, record: dict) -> None:
    """Back-populate the cache with a TTL capped at the key's remaining lifetime. Best-effort."""
    ttl = request.app.state.settings.apikey_cache_ttl_seconds
    expires_at = _parse_ts(record.get("expires_at"))
    if expires_at is not None:
        remaining = int((expires_at - datetime.now(UTC)).total_seconds())
        if remaining <= 0:
            return  # already expired — never cache a snapshot that outlives real expiry
        ttl = min(ttl, remaining)
    try:
        await request.app.state.redis.set(_cache_key(key_hash), json.dumps(record), ex=ttl)
    except RedisError:
        log.warning('{"action": "apikey.cache_put_degraded", "reason": "redis_unavailable"}')


async def _org_cp_verify(request: Request, key_hash: str) -> dict | None:
    """Resolve a hash against Org CP's internal verify endpoint. Returns the record, or None when the
    key is unknown (404). A transport error fails CLOSED (503) — an unverifiable key is never admitted."""
    client: httpx.AsyncClient = request.app.state.org_cp_client
    try:
        resp = await client.get("/internal/v1/api-keys/verify", params={"hash": key_hash})
    except httpx.RequestError as exc:
        raise ServiceUnavailableError("key verification is unavailable") from exc
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise ServiceUnavailableError("key verification is unavailable")
    return resp.json()


def _is_valid(record: dict) -> bool:
    """Re-validate the security-relevant fields against `now` — run on EVERY request, cache hit or
    miss, so a stale cached 'valid' snapshot never admits past the key's real state (ADR-026 §12.7)."""
    if record.get("revoked_at") is not None:
        return False
    expires_at = _parse_ts(record.get("expires_at"))
    if expires_at is not None and expires_at <= datetime.now(UTC):
        return False
    return bool(record.get("owner_active"))


async def verify_api_key(request: Request) -> None:
    """Authenticate a /v1 request by resolving its bearer to a per-org API key (ADR-026). Sets
    `request.state.principal = org_id` so the existing policy chain / rate limiters enforce the
    resolving org's plan (AD-04). Authentication only — no per-key authorization (ADR-028)."""
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise UnauthorizedError(_INVALID)
    key_hash = hash_key(token)

    record = await _cache_get(request, key_hash)
    if record is None:
        record = await _org_cp_verify(request, key_hash)  # 503 on transport error (fail-closed)
        if record is None:
            raise UnauthorizedError(_INVALID)  # unknown key — opaque
        await _cache_put(request, key_hash, record)

    if not _is_valid(record):  # re-validate every request (hit or miss)
        raise UnauthorizedError(_INVALID)

    request.state.principal = str(record["org_id"])  # the tenant whose plan the limiters enforce
    # Attribution only (ADR-028: keys authenticate only, no per-key authorization) — never used
    # for auth/rate-limit/policy decisions, only carried onto emitted events for later billing
    # attribution (add-gateway-usage-attribution).
    request.state.api_key_id = str(record["id"])
    request.state.owner_id = str(record["owner_id"])


async def enforce_policies(request: Request) -> None:
    endpoints = request.app.state.policy_endpoints
    if not endpoints:
        return  # inert: no policies configured -> identical to no chain at all

    ctx = {
        "principal": getattr(request.state, "principal", None),
        "model": await _peek_model(request),
        "path": request.url.path,
    }
    client = request.app.state.policy_client
    fail_open = request.app.state.policy_fail_open

    async def ask(endpoint: str) -> dict:
        try:
            resp = await client.post(endpoint, json=ctx)
            return resp.json()
        except Exception:
            if fail_open:
                return {"decision": "allow"}
            return {"decision": "deny", "status": 503, "type": "policy_unavailable", "reason": f"policy {endpoint} unreachable"}

    results = await asyncio.gather(*(ask(ep) for ep in endpoints))  # parallel, any-deny-wins

    # Generic X-RateLimit-* stamping (ADR-012, docs/policy-plane.md). Record the
    # most-restrictive rate-limit numbers from ANY reply carrying them, on both allow and
    # deny, BEFORE raising — so a 429 also carries the headers. Capability-agnostic: no
    # policy is named here; a pure-ASGI middleware reads request.state.ratelimit and stamps.
    _stash_ratelimit(request, results)
    _stash_ratelimit_scopes(request, endpoints, results)

    for result in results:
        if result.get("decision") == "deny":
            raise PolicyDeniedError(
                result.get("reason", "request denied by policy"),
                status_code=result.get("status", 429),
                error_type=result.get("type", "rate_limit_exceeded"),
                retry_after=result.get("retry_after"),
            )


def _num(x):
    """int/float only (JSON bool is an int subclass and a non-numeric string are rejected)."""
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _stash_ratelimit(request: Request, results: list[dict]) -> None:
    # Candidates: a numeric limit > 0 (0/absent = unlimited, skipped) AND a numeric remaining.
    candidates = [
        r for r in results
        if isinstance(r, dict) and _num(r.get("limit")) and _num(r.get("limit")) > 0 and _num(r.get("remaining")) is not None
    ]
    if not candidates:
        return  # nothing usable => no headers (inert)
    best = min(candidates, key=lambda r: (r["remaining"] / r["limit"], r["remaining"]))
    reset = int(time.time()) + max((int(_num(r.get("reset_after")) or 0) for r in candidates), default=0)
    request.state.ratelimit = {"limit": int(best["limit"]), "remaining": int(best["remaining"]), "reset": reset}


def _stash_ratelimit_scopes(request: Request, endpoints: list[str], results: list[dict]) -> None:
    # Per-scope rate-limit numbers (RPM vs TPM shown as separate meters), stamped ALONGSIDE the
    # merged X-RateLimit-* headers above — never replacing them. Scope name is derived from the
    # policy endpoint URL (e.g. .../rate-limiter-rpm/check -> "rpm"), so any policy whose URL
    # carries no rpm/tpm hint is skipped. `results` is ordered to match `endpoints` (gather
    # preserves order), so they zip cleanly.
    scopes: dict[str, dict] = {}
    for endpoint, r in zip(endpoints, results):
        if not (isinstance(r, dict) and _num(r.get("limit")) and _num(r.get("limit")) > 0 and _num(r.get("remaining")) is not None):
            continue
        lowered = endpoint.lower()
        name = "rpm" if "rpm" in lowered else "tpm" if "tpm" in lowered else None
        if name is None:
            continue
        reset = int(time.time()) + int(_num(r.get("reset_after")) or 0)
        scopes[name] = {"limit": int(r["limit"]), "remaining": int(r["remaining"]), "reset": reset}
    if scopes:
        request.state.ratelimit_scopes = scopes


async def _peek_model(request: Request) -> str | None:
    """Best-effort model from the request body. Starlette caches the body, so the
    route re-reads it at no cost; GET/malformed -> None."""
    try:
        body = await request.json()
    except Exception:
        return None
    return body.get("model") if isinstance(body, dict) else None
