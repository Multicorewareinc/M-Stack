"""RpmConsumer message handling (ADR-013) and start/reconnect resilience (ADR-016). No real
NATS — feed messages to `_on_msg` directly, and for start() inject a fake `nats` module."""

import json
import sys
import types

from consumer import RpmConsumer
from settings import Settings


class SpyService:
    def __init__(self):
        self.calls = []

    async def count(self, principal, model, path, request_id):
        self.calls.append((principal, model, path, request_id))


class FakeMsg:
    def __init__(self, obj):
        self.data = json.dumps(obj).encode() if not isinstance(obj, bytes) else obj
        self.acked = False

    async def ack(self):
        self.acked = True


def _consumer(spy):
    return RpmConsumer(Settings(event_backbone_url="nats://test"), spy)


# --- start()/reconnect fakes (ADR-016) ------------------------------------------------------

class FakeSub:
    def __init__(self):
        self.unsubscribed = False

    async def unsubscribe(self):
        self.unsubscribed = True


class FakeJS:
    """jetstream() stand-in: add_stream is a no-op; subscribe succeeds or raises to exercise
    the durable-conflict / transient classification and the reconnect rebind."""

    def __init__(self, *, subscribe_error=None):
        self._subscribe_error = subscribe_error
        self.subscribe_calls = 0

    async def add_stream(self, **kwargs):
        return None

    async def subscribe(self, *args, **kwargs):
        self.subscribe_calls += 1
        if self._subscribe_error is not None:
            raise self._subscribe_error
        return FakeSub()


class FakeNC:
    def __init__(self, *, connected=True, js=None):
        self.is_connected = connected
        self._js = js or FakeJS()
        self.drained = False

    def jetstream(self):
        return self._js

    async def drain(self):
        self.drained = True


def _install_fake_nats(monkeypatch, *, nc=None, connect_error=None):
    """Inject a fake `nats` module (and `nats.js.api`) so RpmConsumer.start()'s lazy imports
    resolve without nats-py. Returns the recorded connect kwargs holder for callback access."""
    mod = types.ModuleType("nats")
    captured = {}

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


async def test_durable_conflict_is_classified_and_inactive(monkeypatch, caplog):
    spy = SpyService()
    nc = FakeNC(connected=True, js=FakeJS(subscribe_error=Exception("consumer already exists")))
    _install_fake_nats(monkeypatch, nc=nc)
    c = _consumer(spy)
    with caplog.at_level("WARNING"):
        await c.start()  # must not raise
    assert c.active is False
    assert any("durable_conflict" in r.getMessage() for r in caplog.records)
    assert not any("bind_failed" in r.getMessage() for r in caplog.records)


async def test_start_leaves_inactive_on_connect_failure_without_crashing(monkeypatch):
    # nats-py has no first-connect retry option (retry_on_failed_connect was never a real
    # parameter, at any version) -- connect() raises immediately when the backbone is down.
    # start() must swallow that, stay inactive, and schedule a background retry rather than
    # crash or give up forever.
    spy = SpyService()
    _install_fake_nats(monkeypatch, connect_error=ConnectionRefusedError("nats down"))
    scheduled = []
    monkeypatch.setattr("consumer.asyncio.create_task", lambda coro: scheduled.append(coro) or coro.close())
    c = _consumer(spy)
    await c.start()
    assert c.active is False
    assert c._nc is None
    assert len(scheduled) == 1  # a retry was scheduled, not abandoned


async def test_retry_connect_recovers_after_repeated_failures(monkeypatch):
    # Drives _retry_connect() directly (as start() would via asyncio.create_task) against a
    # connect() that fails twice before succeeding, confirming the background loop is what
    # actually recovers a backbone that was down at startup.
    spy = SpyService()
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

    c = _consumer(spy)
    assert c.active is False
    await c._retry_connect()
    assert calls["n"] == 3
    assert c.active is True


async def test_reconnect_rebinds_and_restores_active(monkeypatch):
    spy = SpyService()
    js = FakeJS()  # subscribe succeeds
    nc = FakeNC(connected=False, js=js)  # not connected at start -> bind deferred
    _install_fake_nats(monkeypatch, nc=nc)
    c = _consumer(spy)
    await c.start()
    assert c.active is False

    # The backbone comes up: nats-py fires the reconnect handler, which binds.
    nc.is_connected = True
    await c._on_reconnected()
    assert c.active is True

    # Counting resumes: a request event is now counted.
    await c._on_msg(FakeMsg({"type": "request", "request_id": "r1", "principal": "u", "model": "m", "path": "/p"}))
    assert spy.calls == [("u", "m", "/p", "r1")]

    # A later reconnect drains the live sub before rebinding (no duplicate JS push binding).
    live = c._sub
    await c._on_reconnected()
    assert live.unsubscribed is True
    assert c.active is True
    assert js.subscribe_calls == 2


async def test_request_event_is_counted_and_acked():
    spy = SpyService()
    c = _consumer(spy)
    msg = FakeMsg({"type": "request", "request_id": "r1", "principal": "u1", "model": "m1", "path": "/v1/chat/completions"})
    await c._on_msg(msg)
    assert spy.calls == [("u1", "m1", "/v1/chat/completions", "r1")]
    assert msg.acked is True


async def test_response_event_is_not_counted_but_acked():
    spy = SpyService()
    c = _consumer(spy)
    msg = FakeMsg({"type": "response", "request_id": "r1", "status": 200})
    await c._on_msg(msg)
    assert spy.calls == []          # only `request` events feed RPM
    assert msg.acked is True


async def test_bad_message_is_not_counted_but_acked():
    spy = SpyService()
    c = _consumer(spy)
    msg = FakeMsg(b"{not json")
    await c._on_msg(msg)
    assert spy.calls == []
    assert msg.acked is True         # poison message can't stall the consumer
