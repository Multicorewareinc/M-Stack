"""Covers specs/billing-metering/spec.md "Resilient, off-hot-path consumer": backbone down at
startup does not crash, and the durable binds to its NACK-provisioned consumer (ADR-016
pattern), now over the multistack.queue SDK (ADR-033).

Reconnect is no longer this service's job: the SDK's own NATS client reconnects and restores
the push subscription, so there is no rebind hook to drive here."""

from __future__ import annotations

from conftest import FakeQueue, install_fake_queue, response_event


async def test_start_leaves_inactive_on_connect_failure_without_crashing(consumer_factory, monkeypatch):
    # A backbone that fails to connect must be swallowed: stay inactive, schedule a
    # background retry, never crash or give up forever.
    consumer, _engine, _sm = await consumer_factory(event_backbone_url="nats://test")
    install_fake_queue(monkeypatch, connect_error=ConnectionRefusedError("backbone down"))
    scheduled = []
    monkeypatch.setattr("consumer.asyncio.create_task", lambda coro: scheduled.append(coro) or coro.close())
    await consumer.start()
    assert consumer.active is False
    assert consumer._queue is None
    assert len(scheduled) == 1


async def test_retry_connect_recovers_after_repeated_failures(consumer_factory, monkeypatch):
    consumer, _engine, _sm = await consumer_factory(event_backbone_url="nats://test")
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


async def test_bind_uses_the_nack_provisioned_consumer_and_meters(consumer_factory, monkeypatch):
    """Binds by stream + durable with ON_SUCCESS acking — the ack policy that makes a raising
    handler redeliver rather than silently drop billable usage. Metering works once bound."""
    from multistack.queue.drivers.nats.messaging import AckMode

    consumer, _engine, sessionmaker = await consumer_factory(event_backbone_url="nats://test")
    queue = FakeQueue()
    install_fake_queue(monkeypatch, queue=queue)

    await consumer.start()
    assert consumer.active is True
    assert queue.subscribe_calls == 1
    binding = queue.bindings[0]
    assert binding.stream == "GATEWAY_EVENTS_ENRICHED"
    assert binding.durable == "billing-metering"
    assert binding.ack_mode is AckMode.ON_SUCCESS

    await consumer._on_msg(response_event(request_id="after-bind"))

    from sqlalchemy import select
    from models import Usage

    async with sessionmaker() as s:
        rows = list(await s.scalars(select(Usage).where(Usage.request_id == "after-bind")))
        assert len(rows) == 1


async def test_durable_conflict_is_classified_and_inactive(consumer_factory, monkeypatch, caplog):
    consumer, _engine, _sm = await consumer_factory(event_backbone_url="nats://test")
    install_fake_queue(monkeypatch, queue=FakeQueue(subscribe_error=Exception("consumer already exists")))
    with caplog.at_level("WARNING"):
        await consumer.start()  # must not raise
    assert consumer.active is False
    assert any("durable_conflict" in r.getMessage() for r in caplog.records)
