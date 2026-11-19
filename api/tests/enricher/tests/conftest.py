"""Test helpers for the enricher: a fake multistack.queue Queue that captures republishes and
honors the message_id dedup a real backbone applies, a MockTransport tokenizer client with an
invocation counter, and the start()/retry fakes — so tests never touch a live backbone, the
real SDK driver, or a real tokenizer service (ADR-033)."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from consumer import EnricherConsumer
from main import create_app
from settings import Settings


class FakeSub:
    def __init__(self) -> None:
        self.unsubscribed = False

    async def unsubscribe(self) -> None:
        self.unsubscribed = True


class FakeQueue:
    """A fake multistack.queue.Queue: captures publish() calls and simulates the message_id
    dedup a real backbone applies within its window; records subscribe calls/bindings.

    Note the enricher uses ONE Queue for both roles, which this mirrors."""

    name = "nats"

    def __init__(self, *, publish_error: Exception | None = None, subscribe_error: Exception | None = None) -> None:
        self._publish_error = publish_error
        self._subscribe_error = subscribe_error
        self.published: list[tuple[str, dict, str | None]] = []  # (subject, event, message_id)
        self._seen_msg_ids: set[str] = set()
        self.subscribe_calls = 0
        self.bindings: list = []
        self.closed = False
        self.connected = True

    async def connect(self) -> None:
        return None

    async def publish(self, destination: str, message: dict, *, options=None):
        if self._publish_error is not None:
            raise self._publish_error  # propagates -> enricher re-raises -> no ack (redelivery)
        msg_id = options.message_id if options is not None else None
        if msg_id is not None and msg_id in self._seen_msg_ids:
            return None  # dedup: duplicate within the window is dropped
        if msg_id is not None:
            self._seen_msg_ids.add(msg_id)
        self.published.append((destination, message, msg_id))
        return None

    async def subscribe(self, binding, handler):
        self.subscribe_calls += 1
        self.bindings.append(binding)
        if self._subscribe_error is not None:
            raise self._subscribe_error
        return FakeSub()

    async def close(self) -> None:
        self.closed = True


def _install_fake_queue(monkeypatch, *, queue=None, connect_error=None) -> None:
    """Patch multistack.queue.Queue so start()/_retry_connect resolve to our fake Queue (or
    raise) — no live backbone needed. The consumer imports it lazily inside those methods."""
    import multistack.queue as mq

    class _FakeQueueFactory:
        def __init__(self, config):
            self._q = queue

        async def connect(self):
            if connect_error is not None:
                raise connect_error
            return None

        def __getattr__(self, item):
            return getattr(self._q, item)

    monkeypatch.setattr(mq, "Queue", _FakeQueueFactory)


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
    queue: FakeQueue | None = None,
    **settings_kwargs,
):
    """Build an EnricherConsumer wired to a capturing FakeQueue and a MockTransport
    tokenizer client. Returns (consumer, fake_queue, tokenizer_calls)."""
    settings = Settings(**settings_kwargs)
    client, calls = make_tokenizer_client(tokens=tokenizer_tokens, raises=tokenizer_raises)
    fake_queue = queue or FakeQueue(publish_error=publish_error)
    consumer = EnricherConsumer(settings, http_client=client, queue=fake_queue)
    return consumer, fake_queue, calls


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
