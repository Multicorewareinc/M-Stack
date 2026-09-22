"""Test helpers: build the app with an injected in-memory async fake Valkey, so
tests never touch a real Valkey (mirrors the gateway's injected-client pattern)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from main import create_app
from settings import Settings


class FakePipeline:
    def __init__(self, redis: "FakeRedis") -> None:
        self._redis = redis
        self._ops: list[tuple] = []

    def incr(self, key: str) -> "FakePipeline":
        self._ops.append(("incr", key))
        return self

    def expire(self, key: str, ttl: int) -> "FakePipeline":
        self._ops.append(("expire", key, ttl))
        return self

    async def execute(self) -> list:
        if self._redis.fail:
            raise RuntimeError("redis down")
        results = []
        for op in self._ops:
            if op[0] == "incr":
                self._redis.store[op[1]] = self._redis.store.get(op[1], 0) + 1
                results.append(self._redis.store[op[1]])
            else:  # expire
                results.append(True)
        return results


class FakeRedis:
    """Minimal async fake: the pipeline() + mget() + set() surface the service uses."""

    def __init__(self, *, fail: bool = False) -> None:
        self.store: dict[str, int] = {}
        self.fail = fail

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self)

    async def mget(self, keys: list[str]) -> list:
        if self.fail:
            raise RuntimeError("redis down")
        return [self.store.get(k) for k in keys]

    async def set(self, key: str, value, *, nx: bool = False, ex: int | None = None):
        if self.fail:
            raise RuntimeError("redis down")
        if nx and key in self.store:
            return None  # mimic redis SET NX miss
        self.store[key] = value
        return True

    async def aclose(self) -> None:  # pragma: no cover - lifespan close on owned client
        pass


class FakeConsumer:
    """Stand-in for RpmConsumer exposing only the `active` property /ready + the gauge read.
    Injecting it makes `own_consumer` false, so the app's lifespan never tries to start it."""

    def __init__(self, *, active: bool) -> None:
        self._active = active

    @property
    def active(self) -> bool:
        return self._active


def make_client(
    *, fail: bool = False, redis: FakeRedis | None = None, consumer=None, **settings_kwargs
):
    """Build the app with an injected FakeRedis and (optionally) a fake consumer.
    Returns (TestClient, FakeRedis)."""
    fake = redis or FakeRedis(fail=fail)
    app = create_app(Settings(**settings_kwargs), redis_client=fake, consumer=consumer)
    return TestClient(app, raise_server_exceptions=False), fake


def make_service(*, fail: bool = False, redis: FakeRedis | None = None, **settings_kwargs):
    """Build the RateLimiterService directly (async unit tests). Returns (service, FakeRedis)."""
    from service import RateLimiterService

    fake = redis or FakeRedis(fail=fail)
    return RateLimiterService(Settings(**settings_kwargs), redis_client=fake), fake


@pytest.fixture
def client_factory():
    return make_client


@pytest.fixture
def service_factory():
    return make_service
