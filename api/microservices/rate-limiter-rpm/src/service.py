"""RPM logic. Fixed 60s epoch-minute window in shared Valkey (ADR-005), counted
**asynchronously** off the event backbone (ADR-013, supersedes ADR-010 for RPM).

Two halves, deliberately split (ADR-010's sync/async split, now applied to RPM too):

- `check(...)`  — the synchronous `/check` seam. **Read-only.** For each applicable scope
  it GETs the current per-minute counter and denies if it is at/over the limit. It never
  writes, so a burst can slightly overshoot before its `request` events are counted
  (soft limit — accepted for the async design).
- `count(...)` — called by the NATS consumer for each gateway `request` event. It INCRs +
  EXPIREs the per-scope counters, deduped on `request_id` so at-least-once redelivery
  (ADR-008) never double-counts.

Valkey errors fail open: `/check` allows, `count` drops the increment. The limiter never
blocks the gateway on its own storage/backbone fault (AD confirmed).
"""

from __future__ import annotations

import logging
import time

from settings import RateLimits, Settings

logger = logging.getLogger(__name__)

KEY_PREFIX = "rl:rpm"


class RateLimiterService:
    def __init__(self, settings: Settings, *, redis_client, plan_client=None) -> None:
        self._limits: RateLimits = settings.limits()
        self._redis = redis_client
        self._dedupe_ttl = settings.dedupe_ttl
        self._plan_client = plan_client

    async def _scopes(self, principal: str | None, model: str | None) -> list[tuple[str, str, int]]:
        """Applicable (name, identity, limit) tuples. A scope is applicable only when it has
        a configured non-zero limit; 0/absent => unlimited => skipped.

        The "user" scope's identity is the org_id (AD-04: `principal` IS the org). Its limit
        prefers the org's actual plan (resolved via `plan_client`, cached briefly there) over
        the static RATE_LIMITS user_default/user_overrides, which only apply when the plan
        lookup is inert/unresolved (no admin_cp_url configured, or the lookup failed/degraded)."""
        lim = self._limits
        out: list[tuple[str, str, int]] = []
        if principal:
            plan_limit = await self._plan_client.get_rpm_limit(principal) if self._plan_client else None
            n = plan_limit if plan_limit is not None else lim.user_overrides.get(principal, lim.user_default)
            if n:
                out.append(("user", principal, n))
        if model:
            n = lim.model_overrides.get(model, lim.model_default)
            if n:
                out.append(("model", model, n))
        if principal and model:
            n = lim.user_model_overrides.get(f"{principal}|{model}")  # no default for this scope
            if n:
                out.append(("user_model", f"{principal}|{model}", n))
        return out

    @staticmethod
    def _key(name: str, ident: str, minute: int) -> str:
        return f"{KEY_PREFIX}:{name}:{ident}:{minute}"

    # --- synchronous seam: READ-ONLY allow/deny --------------------------------
    async def check(self, principal: str | None, model: str | None, path: str | None) -> dict:
        scopes = await self._scopes(principal, model)
        if not scopes:
            return {"decision": "allow"}  # nothing configured => unlimited, no Redis touched

        minute = int(time.time()) // 60
        keys = [self._key(name, ident, minute) for name, ident, _ in scopes]

        try:
            values = await self._redis.mget(keys)  # read-only; counting happens async (count())
        except Exception:
            logger.warning("redis_error_fail_open", exc_info=True)
            return {"decision": "allow"}

        reset_after = 60 - int(time.time()) % 60  # seconds to the minute boundary
        # (name, limit, count, remaining). count reflects requests counted SO FAR (async),
        # so it may lag — the soft-limit property of ADR-013.
        info = [
            (name, limit, int(v or 0), max(0, limit - int(v or 0)))
            for (name, _ident, limit), v in zip(scopes, values)
        ]
        denied = [s for s in info if s[2] >= s[1]]  # count at or over the limit => deny

        def _fields(scope) -> dict:
            _name, limit, _count, remaining = scope
            return {"limit": limit, "remaining": remaining, "reset_after": reset_after}

        def _most_restrictive(scopes_info):
            return min(scopes_info, key=lambda s: (s[3] / s[1], s[3]))  # (remaining/limit, remaining)

        if denied:
            worst = _most_restrictive(denied)
            return {
                "decision": "deny",
                "status": 429,
                "type": "rate_limit_exceeded",
                "retry_after": reset_after,
                "reason": f"{worst[0].replace('_', '+')} rpm exceeded",
                **_fields(worst),
            }
        return {"decision": "allow", **_fields(_most_restrictive(info))}

    # --- asynchronous counting: called by the backbone consumer ----------------
    async def count(self, principal: str | None, model: str | None, path: str | None, request_id: str | None) -> None:
        """INCR the per-scope counters for one request event. Idempotent on request_id
        (ADR-008): a redelivered event is counted at most once. Best-effort — Valkey errors
        drop the increment (soft)."""
        scopes = await self._scopes(principal, model)
        if not scopes:
            return  # unlimited scopes => nothing to count

        if request_id:
            try:
                # SETNX + TTL: first sighting returns True; a replay returns None/False -> skip.
                first = await self._redis.set(f"{KEY_PREFIX}:seen:{request_id}", 1, nx=True, ex=self._dedupe_ttl)
            except Exception:
                logger.warning("dedupe_error_counting_anyway", exc_info=True)
                first = True
            if not first:
                return  # already counted this request

        minute = int(time.time()) // 60
        try:
            pipe = self._redis.pipeline()
            for name, ident, _ in scopes:
                key = self._key(name, ident, minute)
                pipe.incr(key)
                pipe.expire(key, 60)
            await pipe.execute()
        except Exception:
            logger.warning("count_redis_error_dropped", exc_info=True)
