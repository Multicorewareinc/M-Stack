"""Covers specs/event-enricher/spec.md "Resilient, off-hot-path consumer": backbone down at
startup does not crash, and the durable rebinds on reconnect (ADR-016 pattern)."""

from __future__ import annotations

import sys
import types

from fastapi.testclient import TestClient

from conftest import FakeMsg, FakeNC, FakeJS, _install_fake_nats, response_event
from main import create_app
from settings import Settings


async def test_start_leaves_inactive_on_connect_failure_without_crashing(consumer_factory, monkeypatch):
    # nats-py has no first-connect retry option (retry_on_failed_connect was never a real
    # parameter, at any version) -- connect() raises immediately when the backbone is down.
    # start() must swallow that, stay inactive, and schedule a background retry rather than
    # crash or give up forever.
    consumer, _js, _calls = consumer_factory()
    _install_fake_nats(monkeypatch, connect_error=ConnectionRefusedError("nats down"))
    scheduled = []
    monkeypatch.setattr("consumer.asyncio.create_task", lambda coro: scheduled.append(coro) or coro.close())
    await consumer.start()
    assert consumer.active is False
    assert consumer._nc is None
    assert len(scheduled) == 1


async def test_retry_connect_recovers_after_repeated_failures(consumer_factory, monkeypatch):
    consumer, _js, _calls = consumer_factory()
    js = FakeJS()
    nc = FakeNC(connected=True, js=js)
    calls = {"n": 0}

    async def flaky_connect(url, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionRefusedError("nats down")
        return nc

    mod = types.ModuleType("nats")
    mod.connect = flaky_connect
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

    async def instant_sleep(_seconds):
        return None

    monkeypatch.setattr("consumer.asyncio.sleep", instant_sleep)

    assert consumer.active is False
    await consumer._retry_connect()
    assert calls["n"] == 3
    assert consumer.active is True


async def test_reconnect_rebinds_and_resumes(consumer_factory, monkeypatch):
    consumer, _js, _calls = consumer_factory()
    js = FakeJS()
    nc = FakeNC(connected=False, js=js)
    _install_fake_nats(monkeypatch, nc=nc)
    await consumer.start()
    assert consumer.active is False

    nc.is_connected = True
    await consumer._on_reconnected()
    assert consumer.active is True

    # Republishing resumes after rebind.
    await consumer._on_msg(FakeMsg(response_event(request_id="after-reconnect")))
    assert any(e["request_id"] == "after-reconnect" for _s, e, _h in js.published)

    # A later reconnect drains the live sub before rebinding (no duplicate JS push binding).
    live = consumer._sub
    await consumer._on_reconnected()
    assert live.unsubscribed is True
    assert consumer.active is True
    assert js.subscribe_calls == 2


def test_backbone_down_at_startup_does_not_crash(monkeypatch):
    # A backbone that is unreachable at startup must leave the service up and /health green.
    _install_fake_nats(monkeypatch, connect_error=RuntimeError("nats unreachable"))
    app = create_app(Settings(event_backbone_url="nats://unreachable:4222"))
    with TestClient(app, raise_server_exceptions=False) as client:  # runs lifespan (start/stop)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        assert app.state.consumer.active is False  # inactive, not crashed
