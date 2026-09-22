"""Unit: modules/auth/lockout — threshold, exponential backoff, clear-on-success, degrade-open,
counter-write non-fatal. Uses fakeredis.aioredis. Maps spec 'Brute-force lockout' + task 9.3."""

from __future__ import annotations

import fakeredis.aioredis
import pytest
from redis.exceptions import RedisError

from errors import TooManyAttemptsError
from modules.auth import lockout
from settings import Settings

EMAIL = "victim@platform.local"
IP = "203.0.113.9"


def _settings(**kw):
    base = {
        "login_max_attempts": 3,
        "login_lockout_window_seconds": 900,
        "login_backoff_base_seconds": 30,
        "login_backoff_ceiling_seconds": 3600,
        "login_lockout_fail_open": True,
        "jwt_secret": "x" * 32,
    }
    base.update(kw)
    return Settings(**base)


def _use_fake(monkeypatch):
    fake = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(lockout, "get_redis", lambda settings: fake)
    return fake


async def test_threshold_trips_with_retry_after(monkeypatch):
    _use_fake(monkeypatch)
    s = _settings()
    # Below threshold: not locked.
    for _ in range(s.login_max_attempts - 1):
        await lockout.record_failure(EMAIL, IP, s)
    await lockout.check_locked(EMAIL, IP, s)  # no raise
    # The threshold-tripping failure writes the lock marker.
    await lockout.record_failure(EMAIL, IP, s)
    with pytest.raises(TooManyAttemptsError) as ei:
        await lockout.check_locked(EMAIL, IP, s)
    assert ei.value.retry_after > 0
    assert ei.value.retry_after <= s.login_backoff_base_seconds  # first cycle backoff == base


async def test_backoff_escalates_by_cycle(monkeypatch):
    fake = _use_fake(monkeypatch)
    s = _settings()
    # Trip the first lock (cycle 1 → base backoff = 30).
    for _ in range(s.login_max_attempts):
        await lockout.record_failure(EMAIL, IP, s)
    ttl1 = await fake.ttl(lockout._key(EMAIL, IP, "lock"))
    assert ttl1 <= 30
    # Clear the lock+fail counters (cycle counter intentionally preserved), then re-trip: cycle 2 →
    # base * 2^(2-1) = 60.
    await lockout.clear(EMAIL, IP, s)
    for _ in range(s.login_max_attempts):
        await lockout.record_failure(EMAIL, IP, s)
    ttl2 = await fake.ttl(lockout._key(EMAIL, IP, "lock"))
    assert 30 < ttl2 <= 60


async def test_clear_on_success(monkeypatch):
    _use_fake(monkeypatch)
    s = _settings()
    for _ in range(s.login_max_attempts):
        await lockout.record_failure(EMAIL, IP, s)
    with pytest.raises(TooManyAttemptsError):
        await lockout.check_locked(EMAIL, IP, s)
    await lockout.clear(EMAIL, IP, s)
    await lockout.check_locked(EMAIL, IP, s)  # cleared → no raise


async def test_degrade_open_on_redis_error(monkeypatch):
    def boom(settings):
        raise RedisError("down")

    monkeypatch.setattr(lockout, "get_redis", boom)
    # fail_open True → check_locked returns (login proceeds).
    await lockout.check_locked(EMAIL, IP, _settings(login_lockout_fail_open=True))
    # fail_open False → degrade-closed (re-raises the RedisError → 500).
    with pytest.raises(RedisError):
        await lockout.check_locked(EMAIL, IP, _settings(login_lockout_fail_open=False))


async def test_counter_write_failure_non_fatal(monkeypatch):
    def boom(settings):
        raise RedisError("down")

    monkeypatch.setattr(lockout, "get_redis", boom)
    # record_failure and clear swallow a Valkey outage regardless of the fail-open flag.
    await lockout.record_failure(EMAIL, IP, _settings(login_lockout_fail_open=False))
    await lockout.clear(EMAIL, IP, _settings(login_lockout_fail_open=False))
