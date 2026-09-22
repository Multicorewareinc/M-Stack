"""Covers specs/rate-limiter-tpm/spec.md: the consumer's plain usage-read decision tree
(ADR-030 — no tokenizer fallback in this service), that non-response events are ignored,
start/reconnect resilience (ADR-016), and the AD-07 startup validation of the
enriched-subject dependency."""

from __future__ import annotations

import json
import sys
import time
import types

import pytest

from main import create_app
from settings import Settings


class FakeMsg:
    def __init__(self, event: dict) -> None:
        self.data = json.dumps(event).encode()
        self.acked = False

    async def ack(self) -> None:
        self.acked = True


def _minute() -> int:
    return int(time.time()) // 60


async def test_usage_present_counts_total_tokens(consumer_factory):
    consumer, _service, redis = consumer_factory(rate_limits='{"user_default":1000}')
    event = {
        "type": "response",
        "request_id": "req-1",
        "principal": "u1",
        "model": "m1",
        "usage": {"total_tokens": 150, "source": "provider"},
    }
    msg = FakeMsg(event)
    await consumer._on_msg(msg)
    assert redis.store[f"rl:tpm:user:u1:{_minute()}"] == 150
    assert msg.acked


async def test_usage_estimated_source_counts_same_as_provider(consumer_factory):
    consumer, _service, redis = consumer_factory(rate_limits='{"user_default":1000}')
    event = {
        "type": "response",
        "request_id": "req-2b",
        "principal": "u1",
        "model": "m1",
        "usage": {"total_tokens": 42, "source": "estimated"},
    }
    msg = FakeMsg(event)
    await consumer._on_msg(msg)
    # Identical treatment to a source:"provider" event — the consumer never branches on source.
    assert redis.store[f"rl:tpm:user:u1:{_minute()}"] == 42
    assert msg.acked


async def test_usage_source_none_skips_counting(consumer_factory):
    consumer, _service, redis = consumer_factory(rate_limits='{"user_default":1000}')
    event = {
        "type": "response",
        "request_id": "req-3",
        "principal": "u1",
        "model": "m1",
        "usage": {"total_tokens": None, "source": "none", "reason": "no_body"},
    }
    msg = FakeMsg(event)
    await consumer._on_msg(msg)
    assert redis.store == {}
    assert msg.acked


async def test_usage_key_missing_entirely_skips_counting(consumer_factory):
    consumer, _service, redis = consumer_factory(rate_limits='{"user_default":1000}')
    event = {"type": "response", "request_id": "req-3b", "principal": "u1", "model": "m1"}
    msg = FakeMsg(event)
    await consumer._on_msg(msg)
    assert redis.store == {}
    assert msg.acked


async def test_total_tokens_non_numeric_skips_counting(consumer_factory):
    consumer, _service, redis = consumer_factory(rate_limits='{"user_default":1000}')
    event = {
        "type": "response",
        "request_id": "req-3c",
        "principal": "u1",
        "model": "m1",
        "usage": {"total_tokens": "not-a-number", "source": "none"},
    }
    msg = FakeMsg(event)
    await consumer._on_msg(msg)
    assert redis.store == {}
    assert msg.acked


def test_no_tokenizer_dependency_exists():
    import consumer as consumer_module

    # This service has no tokenizer/HTTP dependency at all (ADR-030 AD-02): the consumer
    # constructor takes no http_client/tokenizer-shaped parameter, and the module imports
    # no httpx.
    assert "http_client" not in consumer_module.TpmConsumer.__init__.__code__.co_varnames
    assert not hasattr(consumer_module, "httpx")
    import inspect

    src = inspect.getsource(consumer_module)
    assert "import httpx" not in src
    assert "count_via_tokenizer" not in src


async def test_request_event_is_not_counted_but_acked(consumer_factory):
    consumer, _service, redis = consumer_factory(rate_limits='{"user_default":1000}')
    event = {
        "type": "request",
        "request_id": "req-5",
        "principal": "u1",
        "model": "m1",
    }
    msg = FakeMsg(event)
    await consumer._on_msg(msg)
    assert redis.store == {}
    assert msg.acked


# --- start()/reconnect fakes (ADR-016) ------------------------------------------------------

class FakeSub:
    def __init__(self):
        self.unsubscribed = False

    async def unsubscribe(self):
        self.unsubscribed = True


class FakeJS:
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

    def jetstream(self):
        return self._js

    async def drain(self):
        pass


def _install_fake_nats(monkeypatch, *, nc=None, connect_error=None):
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


async def test_durable_conflict_is_classified_and_inactive(consumer_factory, monkeypatch, caplog):
    consumer, _service, _redis = consumer_factory(rate_limits='{"user_default":1000}')
    _install_fake_nats(
        monkeypatch, nc=FakeNC(connected=True, js=FakeJS(subscribe_error=Exception("consumer already exists")))
    )
    with caplog.at_level("WARNING"):
        await consumer.start()  # must not raise
    assert consumer.active is False
    assert any("durable_conflict" in r.getMessage() for r in caplog.records)
    assert not any("bind_failed" in r.getMessage() for r in caplog.records)


async def test_start_leaves_inactive_on_connect_failure_without_crashing(consumer_factory, monkeypatch):
    # nats-py has no first-connect retry option (retry_on_failed_connect was never a real
    # parameter, at any version) -- connect() raises immediately when the backbone is down.
    # start() must swallow that, stay inactive, and schedule a background retry rather than
    # crash or give up forever.
    consumer, _service, _redis = consumer_factory(rate_limits='{"user_default":1000}')
    _install_fake_nats(monkeypatch, connect_error=ConnectionRefusedError("nats down"))
    scheduled = []
    monkeypatch.setattr("consumer.asyncio.create_task", lambda coro: scheduled.append(coro) or coro.close())
    await consumer.start()
    assert consumer.active is False
    assert consumer._nc is None
    assert len(scheduled) == 1


async def test_retry_connect_recovers_after_repeated_failures(consumer_factory, monkeypatch):
    consumer, _service, _redis = consumer_factory(rate_limits='{"user_default":1000}')
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


async def test_reconnect_rebinds_and_restores_active(consumer_factory, monkeypatch):
    consumer, _service, redis = consumer_factory(rate_limits='{"user_default":1000}')
    js = FakeJS()
    nc = FakeNC(connected=False, js=js)
    _install_fake_nats(monkeypatch, nc=nc)
    await consumer.start()
    assert consumer.active is False

    nc.is_connected = True
    await consumer._on_reconnected()
    assert consumer.active is True

    # Counting resumes: a response event with usage is now counted.
    await consumer._on_msg(
        FakeMsg({"type": "response", "request_id": "r1", "principal": "u1", "model": "m1",
                 "usage": {"total_tokens": 150, "source": "provider"}})
    )
    assert redis.store[f"rl:tpm:user:u1:{_minute()}"] == 150

    # A later reconnect drains the live sub before rebinding (no duplicate JS push binding).
    live = consumer._sub
    await consumer._on_reconnected()
    assert live.unsubscribed is True
    assert consumer.active is True
    assert js.subscribe_calls == 2


# --- AD-07 startup validation of the enriched-subject dependency ---------------------------

def test_default_subject_is_enriched_and_valid():
    from conftest import FakeRedis

    app = create_app(Settings(event_backbone_url="nats://x"), redis_client=FakeRedis())
    assert app is not None


def test_raw_subject_with_backbone_configured_fails_fast():
    with pytest.raises(RuntimeError):
        create_app(Settings(event_backbone_url="nats://x", event_stream_subject="gateway.events"))


def test_arbitrary_or_mismatched_subject_fails_fast():
    # (a) an arbitrary/typo'd subject unrelated to either known value
    with pytest.raises(RuntimeError):
        create_app(Settings(event_backbone_url="nats://x", event_stream_subject="something.else"))
    # (b) a mismatched pair: enriched subject, but the raw stream name
    with pytest.raises(RuntimeError):
        create_app(
            Settings(
                event_backbone_url="nats://x",
                event_stream_subject="gateway.events.enriched",
                event_stream_name="GATEWAY_EVENTS",
            )
        )


def test_no_validation_when_backbone_empty_even_with_raw_subject():
    from conftest import FakeRedis

    # No backbone configured => nothing to validate, regardless of a simultaneously
    # misconfigured subject (distinct from test_ops.py's inert-path coverage, which never
    # sets a misconfigured subject at all).
    app = create_app(
        Settings(event_backbone_url="", event_stream_subject="gateway.events"),
        redis_client=FakeRedis(),
    )
    assert app is not None
