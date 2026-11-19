"""Covers specs/rate-limiter-tpm/spec.md: the consumer's plain usage-read decision tree
(ADR-030 — no tokenizer fallback in this service), that non-response events are ignored,
start resilience (ADR-016), and the AD-07 startup validation of the enriched-subject
dependency.

Post-ADR-033 the handler receives an already-deserialized dict and never acknowledges — the
multistack.queue SDK owns deserialization and ack (AckMode.ALWAYS here), so these tests call
`_on_msg(event)` directly with a plain dict."""

from __future__ import annotations

import time

import pytest

from main import create_app
from settings import Settings


def _minute() -> int:
    return int(time.time()) // 60


async def test_usage_present_counts_total_tokens(consumer_factory):
    consumer, _service, redis = consumer_factory(tpm_rate_limits='{"user_default":1000}')
    event = {
        "type": "response",
        "request_id": "req-1",
        "principal": "u1",
        "model": "m1",
        "usage": {"total_tokens": 150, "source": "provider"},
    }
    await consumer._on_msg(event)
    assert redis.store[f"rl:tpm:user:u1:{_minute()}"] == 150


async def test_usage_estimated_source_counts_same_as_provider(consumer_factory):
    consumer, _service, redis = consumer_factory(tpm_rate_limits='{"user_default":1000}')
    event = {
        "type": "response",
        "request_id": "req-2b",
        "principal": "u1",
        "model": "m1",
        "usage": {"total_tokens": 42, "source": "estimated"},
    }
    await consumer._on_msg(event)
    # Identical treatment to a source:"provider" event — the consumer never branches on source.
    assert redis.store[f"rl:tpm:user:u1:{_minute()}"] == 42


async def test_usage_source_none_skips_counting(consumer_factory):
    consumer, _service, redis = consumer_factory(tpm_rate_limits='{"user_default":1000}')
    event = {
        "type": "response",
        "request_id": "req-3",
        "principal": "u1",
        "model": "m1",
        "usage": {"total_tokens": None, "source": "none", "reason": "no_body"},
    }
    await consumer._on_msg(event)
    assert redis.store == {}


async def test_usage_key_missing_entirely_skips_counting(consumer_factory):
    consumer, _service, redis = consumer_factory(tpm_rate_limits='{"user_default":1000}')
    event = {"type": "response", "request_id": "req-3b", "principal": "u1", "model": "m1"}
    await consumer._on_msg(event)
    assert redis.store == {}


async def test_total_tokens_non_numeric_skips_counting(consumer_factory):
    consumer, _service, redis = consumer_factory(tpm_rate_limits='{"user_default":1000}')
    event = {
        "type": "response",
        "request_id": "req-3c",
        "principal": "u1",
        "model": "m1",
        "usage": {"total_tokens": "not-a-number", "source": "none"},
    }
    await consumer._on_msg(event)
    assert redis.store == {}


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
    consumer, _service, redis = consumer_factory(tpm_rate_limits='{"user_default":1000}')
    event = {
        "type": "request",
        "request_id": "req-5",
        "principal": "u1",
        "model": "m1",
    }
    await consumer._on_msg(event)
    assert redis.store == {}


# --- start() fakes (ADR-016) over the multistack.queue SDK (ADR-033) -----------------------

class FakeQueue:
    """A fake multistack.queue.Queue: subscribe succeeds or raises to exercise the
    durable-conflict / transient classification."""

    name = "nats"

    def __init__(self, *, subscribe_error=None):
        self._subscribe_error = subscribe_error
        self.subscribe_calls = 0
        self.bindings = []
        self.closed = False
        self.connected = True

    async def connect(self):
        return None

    async def subscribe(self, binding, handler):
        self.subscribe_calls += 1
        self.bindings.append(binding)
        if self._subscribe_error is not None:
            raise self._subscribe_error
        return object()

    async def close(self):
        self.closed = True


def _install_fake_queue(monkeypatch, *, queue=None, connect_error=None):
    """Patch multistack.queue.Queue so start()/_retry_connect resolve without a live
    backbone. The consumer imports Queue/QueueConfig lazily inside those methods."""
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


async def test_durable_conflict_is_classified_and_inactive(consumer_factory, monkeypatch, caplog):
    consumer, _service, _redis = consumer_factory(tpm_rate_limits='{"user_default":1000}', event_backbone_url="nats://test")
    _install_fake_queue(monkeypatch, queue=FakeQueue(subscribe_error=Exception("consumer already exists")))
    with caplog.at_level("WARNING"):
        await consumer.start()  # must not raise
    assert consumer.active is False
    assert any("durable_conflict" in r.getMessage() for r in caplog.records)
    assert not any("bind_failed" in r.getMessage() for r in caplog.records)


async def test_start_leaves_inactive_on_connect_failure_without_crashing(consumer_factory, monkeypatch):
    # A backbone that fails to connect must be swallowed: stay inactive, schedule a
    # background retry, never crash or give up forever.
    consumer, _service, _redis = consumer_factory(tpm_rate_limits='{"user_default":1000}', event_backbone_url="nats://test")
    _install_fake_queue(monkeypatch, connect_error=ConnectionRefusedError("backbone down"))
    scheduled = []
    monkeypatch.setattr("consumer.asyncio.create_task", lambda coro: scheduled.append(coro) or coro.close())
    await consumer.start()
    assert consumer.active is False
    assert consumer._queue is None
    assert len(scheduled) == 1


async def test_retry_connect_recovers_after_repeated_failures(consumer_factory, monkeypatch):
    consumer, _service, _redis = consumer_factory(tpm_rate_limits='{"user_default":1000}', event_backbone_url="nats://test")
    queue = FakeQueue()
    calls = {"n": 0}

    class _FlakyQueue:
        def __init__(self, config):
            pass

        async def connect(self):
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionRefusedError("backbone down")

        def __getattr__(self, item):
            return getattr(queue, item)

    import multistack.queue as mq

    monkeypatch.setattr(mq, "Queue", _FlakyQueue)

    async def instant_sleep(_seconds):
        return None

    monkeypatch.setattr("consumer.asyncio.sleep", instant_sleep)

    assert consumer.active is False
    await consumer._retry_connect()
    assert calls["n"] == 3
    assert consumer.active is True


async def test_bind_uses_the_nack_provisioned_consumer(consumer_factory, monkeypatch):
    """The binding names the enriched stream + durable and asks for ALWAYS ack (soft-ack);
    the subject/deliver policy/max_deliver live in the NACK consumer, not here."""
    from multistack.queue.drivers.nats.messaging import AckMode

    consumer, _service, redis = consumer_factory(tpm_rate_limits='{"user_default":1000}', event_backbone_url="nats://test")
    queue = FakeQueue()
    _install_fake_queue(monkeypatch, queue=queue)

    await consumer.start()
    assert consumer.active is True
    assert queue.subscribe_calls == 1
    binding = queue.bindings[0]
    assert binding.stream == "GATEWAY_EVENTS_ENRICHED"
    assert binding.durable == "tpm-counter-enriched"
    assert binding.ack_mode is AckMode.ALWAYS

    # Counting works once bound.
    await consumer._on_msg(
        {"type": "response", "request_id": "r1", "principal": "u1", "model": "m1",
         "usage": {"total_tokens": 150, "source": "provider"}}
    )
    assert redis.store[f"rl:tpm:user:u1:{_minute()}"] == 150

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
