"""Test helpers for the enricher: a fake JetStream publisher that captures republishes and
honors Nats-Msg-Id dedup, a MockTransport tokenizer client with an invocation counter, and
the start()/reconnect fakes (mirroring rate-limiter-tpm) — so tests never touch live NATS or
a real tokenizer service."""

from __future__ import annotations

import json
import sys
import types

import httpx
import pytest
from fastapi.testclient import TestClient

from consumer import EnricherConsumer
from main import create_app
from settings import Settings


class FakeMsg:
    """A delivered JetStream message. `num_delivered` drives the terminal-exhaustion path
    (extends rate-limiter-tpm's FakeMsg, which had no metadata)."""

    def __init__(self, event: dict, *, num_delivered: int = 1) -> None:
        self.data = json.dumps(event).encode()
        self.acked = False
        self.metadata = types.SimpleNamespace(num_delivered=num_delivered)

    async def ack(self) -> None:
        self.acked = True


def raw_msg(data: bytes, *, num_delivered: int = 1) -> FakeMsg:
    """A message whose body is arbitrary bytes (for the un-parseable path)."""
    msg = FakeMsg({}, num_delivered=num_delivered)
    msg.data = data
    return msg


class FakeSub:
    def __init__(self) -> None:
        self.unsubscribed = False

    async def unsubscribe(self) -> None:
        self.unsubscribed = True


class FakeJS:
    """Captures publishes and simulates JetStream's Nats-Msg-Id dedup within a window."""

    def __init__(self, *, publish_error: Exception | None = None, subscribe_error: Exception | None = None) -> None:
        self._publish_error = publish_error
        self._subscribe_error = subscribe_error
        self.published: list[tuple[str, dict, dict]] = []  # (subject, event, headers)
        self._seen_msg_ids: set[str] = set()
        self.subscribe_calls = 0

    async def add_stream(self, **kwargs):
        return None

    async def publish(self, subject: str, data: bytes, headers: dict | None = None):
        if self._publish_error is not None:
            raise self._publish_error
        headers = headers or {}
        msg_id = headers.get("Nats-Msg-Id")
        if msg_id is not None and msg_id in self._seen_msg_ids:
            return None  # dedup: duplicate within the window is dropped
        if msg_id is not None:
            self._seen_msg_ids.add(msg_id)
        self.published.append((subject, json.loads(data), headers))
        return None

    async def subscribe(self, *args, **kwargs):
        self.subscribe_calls += 1
        if self._subscribe_error is not None:
            raise self._subscribe_error
        return FakeSub()


class FakeNC:
    def __init__(self, *, connected: bool = True, js: FakeJS | None = None) -> None:
        self.is_connected = connected
        self._js = js or FakeJS()

    def jetstream(self) -> FakeJS:
        return self._js

    async def drain(self) -> None:
        pass


def _install_fake_nats(monkeypatch, *, nc=None, connect_error=None) -> dict:
    mod = types.ModuleType("nats")
    captured: dict = {}

    async def connect(url, **kwargs):
        captured.update(kwargs)
        if connect_error is not None:
            raise connect_error
        return nc

    mod.connect = connect
    js_api = types.ModuleType("nats.js.api")

    class DeliverPolicy:
        NEW = "new"

    class ConsumerConfig:
        def __init__(self, **kwargs):
            pass

    js_api.DeliverPolicy = DeliverPolicy
    js_api.ConsumerConfig = ConsumerConfig
    monkeypatch.setitem(sys.modules, "nats", mod)
    monkeypatch.setitem(sys.modules, "nats.js", types.ModuleType("nats.js"))
    monkeypatch.setitem(sys.modules, "nats.js.api", js_api)
    return captured


def make_tokenizer_client(*, tokens: int = 0, status_code: int = 200, raises: bool = False):
    """httpx.AsyncClient over MockTransport standing in for the tokenizer (the codebase's
    upstream-stubbing convention). Returns (client, calls) where calls['n'] counts invocations
    so a test can assert the provider path makes ZERO tokenizer calls."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if raises:
            raise httpx.ConnectError("tokenizer unreachable", request=request)
        return httpx.Response(status_code, json={"tokens": tokens, "tokenizer": "cl100k_base"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), calls


class StubConsumer:
    """Minimal consumer stand-in for ops tests: exposes only `active` for the gauge."""

    def __init__(self, *, active: bool = True) -> None:
        self._active = active

    @property
    def active(self) -> bool:
        return self._active


def make_consumer(
    *,
    tokenizer_tokens: int = 0,
    tokenizer_raises: bool = False,
    publish_error: Exception | None = None,
    js: FakeJS | None = None,
    **settings_kwargs,
):
    """Build an EnricherConsumer wired to a capturing FakeJS publisher and a MockTransport
    tokenizer client. Returns (consumer, fake_js, tokenizer_calls)."""
    settings = Settings(**settings_kwargs)
    client, calls = make_tokenizer_client(tokens=tokenizer_tokens, raises=tokenizer_raises)
    fake_js = js or FakeJS(publish_error=publish_error)
    consumer = EnricherConsumer(settings, http_client=client, js=fake_js)
    return consumer, fake_js, calls


def make_client(*, consumer=None, **settings_kwargs) -> TestClient:
    """Build the app (optionally with an injected consumer) for ops/endpoint tests."""
    app = create_app(Settings(**settings_kwargs), consumer=consumer)
    return TestClient(app, raise_server_exceptions=False)


def response_event(**overrides) -> dict:
    """A canonical gateway `response` event (events.py::response_event shape)."""
    ev = {
        "type": "response",
        "request_id": "req-1",
        "principal": "u1",
        "model": "m1",
        "status": 200,
        "usage": {"total_tokens": 150},
        "ts": 1234.5,
        "duration_ms": 12.0,
        "stream": False,
        "body": None,
    }
    ev.update(overrides)
    return ev


@pytest.fixture
def consumer_factory():
    return make_consumer


@pytest.fixture
def client_factory():
    return make_client
