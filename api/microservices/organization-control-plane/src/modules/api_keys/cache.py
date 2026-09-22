"""Best-effort Redis eviction of the model-gateway verify cache (ADR-026, reference §12.7).

Reuses the existing `get_redis(settings)` builder (modules/auth/lockout.py) — no new client idiom.
Eviction is best-effort and degrade-open: a RedisError is swallowed and logged, and NEVER turns an
already-committed mutation into a 5xx. Callers MUST evict only AFTER the DB commit (commit-then-evict).
"""

from __future__ import annotations

import logging

from redis.exceptions import RedisError

from modules.auth.lockout import get_redis

log = logging.getLogger(__name__)


def cache_key(key_hash: str) -> str:
    return f"apikey:{key_hash}"


async def evict(settings, key_hash: str) -> None:
    """DEL apikey:{hash}. Swallows Redis outages (logged) — never fails the mutation."""
    try:
        r = get_redis(settings)
        await r.delete(cache_key(key_hash))
    except RedisError:
        log.warning('{"action": "apikey.evict_degraded", "reason": "redis_unavailable"}')
