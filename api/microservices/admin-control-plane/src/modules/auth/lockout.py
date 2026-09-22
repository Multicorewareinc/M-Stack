"""Failed-login lockout & backoff — Redis-backed brute-force defence (leaf).

Three coroutines consumed by the credential path (design D4/D7):

* `check_locked(email, ip, settings)` — raise TooManyAttemptsError (429 + Retry-After) if the key is
  currently locked; run BEFORE the user lookup so a locked attempt costs no DB/hash work.
* `record_failure(email, ip, settings)` — count a credential failure; trip the lock with exponential
  backoff once the threshold is reached.
* `clear(email, ip, settings)` — reset the counter on a successful login.

Keying (design D3): `login:{kind}:{sha256(lower(email))[:32]}:{ip}` — the submitted lowercased email
is hashed (never stored raw), so an existing and a non-existent account are indistinguishable, and
the IP component collapses to per-email behind a proxy with no trusted hops.

Redis is reached via `get_redis(settings)` at call time (never a client bound at import) — patchable
so tests inject a fakeredis client or force a RedisError. Degrade-open (design D7): a RedisError on
the lock check is skipped so login proceeds when `login_lockout_fail_open`; a counter-write failure
is always swallowed and never fails an otherwise-valid login. Ported verbatim from Org CP SP-01.
"""

from __future__ import annotations

import hashlib
import logging

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from errors import TooManyAttemptsError

log = logging.getLogger(__name__)

_FAIL = "fail"
_LOCK = "lock"
_CYCLE = "cycle"


def get_redis(settings) -> aioredis.Redis:
    """Build the async Redis client from settings.valkey_url.

    ponytail: a fresh client per call (ceiling: the lockout path is low-QPS and off the hot path);
    upgrade path = cache one client on app.state if lockout traffic ever warrants pooling. Patched
    in tests to inject a fake / force a RedisError.
    """
    return aioredis.Redis.from_url(settings.valkey_url)


def _key(email: str, ip: str, kind: str) -> str:
    digest = hashlib.sha256(email.lower().encode("utf-8")).hexdigest()[:32]
    return f"login:{kind}:{digest}:{ip}"


def _emit_degraded() -> None:
    log.warning('{"action": "auth.lockout_degraded", "reason": "redis_unavailable"}')


async def check_locked(email: str, ip: str, settings) -> None:
    """Raise TooManyAttemptsError if the email+IP key is currently locked.

    On a Valkey outage: degrade-open (return) when settings.login_lockout_fail_open, else re-raise the
    RedisError (surfacing as a 500 — degrade-closed for high-security deployments).
    """
    try:
        r = get_redis(settings)
        marker = await r.get(_key(email, ip, _LOCK))
        if marker is None:
            return
        ttl = await r.ttl(_key(email, ip, _LOCK))
    except RedisError:
        _emit_degraded()
        if settings.login_lockout_fail_open:
            return
        raise

    if ttl is None or ttl <= 0:
        # The marker expired between GET and TTL — treat as unlocked.
        return
    raise TooManyAttemptsError(retry_after=int(ttl))


async def record_failure(email: str, ip: str, settings) -> None:
    """Count one credential failure; trip the lock once the threshold is reached.

    Increments the windowed failure counter (TTL = window, set on first failure). When it reaches
    login_max_attempts, increments a cycle counter and writes a lock marker for
    `min(base * 2^(cycle-1), ceiling)` seconds (exponential backoff, design D4). A Redis outage is
    swallowed — a counter-write failure must never fail an otherwise-valid login.
    """
    try:
        r = get_redis(settings)
        fail_key = _key(email, ip, _FAIL)
        count = await r.incr(fail_key)
        if count == 1:
            await r.expire(fail_key, settings.login_lockout_window_seconds)
        if count >= settings.login_max_attempts:
            cycle_key = _key(email, ip, _CYCLE)
            cycle = await r.incr(cycle_key)
            # Keep the cycle counter alive across lockouts so repeated lockouts escalate; clear()
            # intentionally leaves it to expire (design D4).
            await r.expire(cycle_key, settings.login_backoff_ceiling_seconds * 2)
            backoff = min(
                settings.login_backoff_base_seconds * (2 ** (int(cycle) - 1)),
                settings.login_backoff_ceiling_seconds,
            )
            await r.set(_key(email, ip, _LOCK), b"1", ex=backoff)
    except RedisError:
        _emit_degraded()


async def clear(email: str, ip: str, settings) -> None:
    """Reset the failure counter and lock marker on a successful login.

    Leaves the cycle counter to expire on its own TTL (design D4) so a rapid re-lock still escalates.
    A Redis outage is swallowed (logged).
    """
    try:
        r = get_redis(settings)
        await r.delete(_key(email, ip, _FAIL), _key(email, ip, _LOCK))
    except RedisError:
        _emit_degraded()
