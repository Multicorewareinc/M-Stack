"""Covers specs/event-enricher/spec.md "Resilient, off-hot-path consumer": backbone down at
startup does not crash, and the durable binds to its NACK-provisioned consumer (ADR-016
pattern), now over the multistack.queue SDK (ADR-033).

Reconnect is no longer this service's job: the SDK's own NATS client reconnects and restores
the push subscription, so there is no rebind hook to drive here."""

from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import FakeQueue, _install_fake_queue, response_event
from main import create_app
from settings import Settings


async def test_start_leaves_inactive_on_connect_failure_without_crashing(consumer_factory, monkeypatch):
    # A backbone that fails to connect must be swallowed: stay inactive, schedule a
    # background retry, never crash or give up forever.
    consumer, _queue, _calls = consumer_factory(event_backbone_url="nats://test")
    _install_fake_queue(monkeypatch, connect_error=ConnectionRefusedError("backbone down"))
    scheduled = []
    monkeypatch.setattr("consumer.asyncio.create_task", lambda coro: scheduled.append(coro) or coro.close())
    await consumer.start()
    assert consumer.active is False
    assert consumer._queue is None
    assert len(scheduled) == 1


async def test_retry_connect_recovers_after_repeated_failures(consumer_factory, monkeypatch):
    consumer, _queue, _calls = consumer_factory(event_backbone_url="nats://test")
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

    consumer._queue = None  # the down-at-startup state start() leaves before scheduling retry
    assert consumer.active is False
    await consumer._retry_connect()
    assert calls["n"] == 3
    assert consumer.active is True  # bound after the third (successful) connect


async def test_bind_uses_the_nack_provisioned_consumer_and_republishes(consumer_factory, monkeypatch):
    """Binds by stream + durable with ON_SUCCESS acking (ADR-030 D5), then republishing works
    over the same single Queue that serves the subscription."""
    from multistack.queue.drivers.nats.messaging import AckMode

    consumer, _queue, _calls = consumer_factory(event_backbone_url="nats://test")
    queue = FakeQueue()
    _install_fake_queue(monkeypatch, queue=queue)

    await consumer.start()
    assert consumer.active is True
    assert queue.subscribe_calls == 1
    binding = queue.bindings[0]
    assert binding.stream == "GATEWAY_EVENTS"
    assert binding.durable == "enricher"
    assert binding.ack_mode is AckMode.ON_SUCCESS

    # One Queue serves both roles: the bound consumer's handler republishes through it.
    await consumer._on_msg(response_event(request_id="after-bind"))
    assert any(e["request_id"] == "after-bind" for _s, e, _mid in queue.published)


async def test_durable_conflict_is_classified_and_inactive(consumer_factory, monkeypatch, caplog):
    consumer, _queue, _calls = consumer_factory(event_backbone_url="nats://test")
    _install_fake_queue(monkeypatch, queue=FakeQueue(subscribe_error=Exception("consumer already exists")))
    with caplog.at_level("WARNING"):
        await consumer.start()  # must not raise
    assert consumer.active is False
    assert any("durable_conflict" in r.getMessage() for r in caplog.records)


def test_backbone_down_at_startup_does_not_crash(monkeypatch):
    # A backbone unreachable at startup must leave the service up and /health green.
    _install_fake_queue(monkeypatch, connect_error=RuntimeError("backbone unreachable"))
    app = create_app(Settings(event_backbone_url="nats://unreachable:4222"))
    with TestClient(app, raise_server_exceptions=False) as client:  # runs lifespan (start/stop)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        assert app.state.consumer.active is False  # inactive, not crashed
