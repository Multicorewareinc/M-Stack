"""Test helpers: build the app with upstream calls routed through an
httpx.MockTransport, so tests never hit the network.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from main import create_app
from settings import Settings

# Fixed org the default verify stub resolves any bearer token to (SP-02: principal = org_id).
VERIFY_ORG_ID = "000000aa-0000-0000-0000-0000000000aa"
VERIFY_OWNER_ID = "000000bb-0000-0000-0000-0000000000bb"
VERIFY_API_KEY_ID = "000000cc-0000-0000-0000-0000000000cc"


class FakeRedis:
    """Tiny in-memory async Redis stub (get/set-with-ex/delete). `boom=True` raises RedisError on
    every op, to exercise the gateway's degrade-open cache path. No fakeredis dependency (ponytail)."""

    def __init__(self, *, boom: bool = False):
        self.boom = boom
        self.store: dict[str, str] = {}
        # Records the `ex` (seconds) each SET was called with, so a test can assert the TTL cap
        # was actually applied — not just that the key ended up cached (council review fix: the
        # old FakeRedis silently discarded `ex`, so no test could ever fail on a wrong TTL).
        self.ex: dict[str, int | None] = {}

    async def get(self, key):
        if self.boom:
            from redis.exceptions import RedisError

            raise RedisError("down")
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        if self.boom:
            from redis.exceptions import RedisError

            raise RedisError("down")
        self.store[key] = value
        self.ex[key] = ex

    async def delete(self, key):
        self.store.pop(key, None)

    async def aclose(self):
        pass


def _verify_record(*, org_id=VERIFY_ORG_ID, owner_id=VERIFY_OWNER_ID, revoked_at=None, expires_at=None, owner_active=True):
    return {
        "id": VERIFY_API_KEY_ID,
        "org_id": org_id, "owner_id": owner_id,
        "revoked_at": revoked_at, "expires_at": expires_at, "owner_active": owner_active,
    }


def _org_cp_valid_handler(request):
    """Default Org CP verify stub: resolves ANY hash to one valid active key (so existing /v1 tests
    authenticate with whatever token they already send)."""
    return httpx.Response(200, json=_verify_record())


def make_verify_clients(*, redis=None, org_cp_handler=_org_cp_valid_handler):
    """Build the (redis, org_cp_client) pair the gateway's verify path needs, with sane defaults."""
    redis = redis if redis is not None else FakeRedis()
    org_cp_client = httpx.AsyncClient(transport=httpx.MockTransport(org_cp_handler), base_url="http://org-cp")
    return redis, org_cp_client


class Recorder:
    """Wraps a response handler and records every upstream request it sees."""

    def __init__(self, factory):
        self.requests: list[httpx.Request] = []
        self._factory = factory

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._factory(request)


def _make(handler=None, **settings_kwargs):
    if handler is None:
        def handler(_req):  # upstream must not be reached in this test
            raise AssertionError("upstream was called unexpectedly")
    recorder = Recorder(handler)
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    redis, org_cp_client = make_verify_clients()
    app = create_app(Settings(**settings_kwargs), client=client, redis=redis, org_cp_client=org_cp_client)
    return TestClient(app, raise_server_exceptions=False), recorder


@pytest.fixture
def make_client():
    return _make


def _make_with_policy(policy_handler, *, upstream_handler=None, policy_endpoints="http://policy/check", **settings_kwargs):
    """Build the app with BOTH the upstream and the policy chain stubbed via
    MockTransport. Returns (TestClient, upstream_recorder, policy_recorder)."""
    settings_kwargs.setdefault("api_key", "test-key")
    if upstream_handler is None:
        def upstream_handler(_req):
            return httpx.Response(200, json={"ok": True})
    up_rec = Recorder(upstream_handler)
    pol_rec = Recorder(policy_handler)
    redis, org_cp_client = make_verify_clients()
    app = create_app(
        Settings(policy_endpoints=policy_endpoints, **settings_kwargs),
        client=httpx.AsyncClient(transport=httpx.MockTransport(up_rec)),
        policy_client=httpx.AsyncClient(transport=httpx.MockTransport(pol_rec)),
        redis=redis, org_cp_client=org_cp_client,
    )
    return TestClient(app, raise_server_exceptions=False), up_rec, pol_rec


@pytest.fixture
def make_policy_client():
    return _make_with_policy


class FakePublisher:
    """In-process stand-in for EventPublisher. Records emitted events. `boom=True` makes
    `emit` raise internally (to prove `emit` is total — it must still never surface)."""

    def __init__(self, *, capture_bodies: bool = False, boom: bool = False):
        self.capture_bodies = capture_bodies
        self.boom = boom
        self.events: list[dict] = []

    def emit(self, event: dict) -> None:
        if self.boom:
            # The real emit wraps its body in try/except so a fault never reaches the
            # request path; mimic that contract here.
            try:
                raise RuntimeError("emit boom")
            except Exception:  # noqa: BLE001
                return
        self.events.append(event)

    async def connect(self) -> None:  # lifespan safety (injected publishers skip connect)
        pass

    async def aclose(self) -> None:
        pass

    def of_type(self, t: str) -> list[dict]:
        return [e for e in self.events if e.get("type") == t]


def _make_with_events(handler=None, *, publisher=None, **settings_kwargs):
    """Build the app with a configured (injected) event publisher and the upstream stubbed
    via MockTransport. Returns (TestClient, publisher, upstream_recorder)."""
    settings_kwargs.setdefault("api_key", "test-key")
    settings_kwargs.setdefault("event_backbone_url", "nats://test")
    if handler is None:
        def handler(_req):
            return httpx.Response(200, json={"ok": True, "usage": {"total_tokens": 7}})
    up_rec = Recorder(handler)
    publisher = publisher or FakePublisher()
    redis, org_cp_client = make_verify_clients()
    app = create_app(
        Settings(**settings_kwargs),
        client=httpx.AsyncClient(transport=httpx.MockTransport(up_rec)),
        event_publisher=publisher,
        redis=redis, org_cp_client=org_cp_client,
    )
    return TestClient(app, raise_server_exceptions=False), publisher, up_rec


@pytest.fixture
def make_events_client():
    return _make_with_events
