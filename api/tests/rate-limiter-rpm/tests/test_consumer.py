"""RpmConsumer message handling (ADR-013) and start resilience (ADR-016), now over the
multistack.queue SDK (ADR-033). No real backbone — the handler takes an already-deserialized
dict (the SDK deserializes and acks), and start() is driven with a fake Queue."""

from consumer import RpmConsumer
from settings import Settings


class SpyService:
    def __init__(self):
        self.calls = []

    async def count(self, principal, model, path, request_id):
        self.calls.append((principal, model, path, request_id))


def _consumer(spy):
    return RpmConsumer(Settings(event_backbone_url="nats://test"), spy)


# --- start() fakes (ADR-016) over the SDK ---------------------------------------------------

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

    made = {}

    class _FakeQueueFactory:
        def __init__(self, config):
            made["config"] = config
            self._q = queue

        async def connect(self):
            if connect_error is not None:
                raise connect_error
            return None

        def __getattr__(self, item):
            return getattr(self._q, item)

    monkeypatch.setattr(mq, "Queue", _FakeQueueFactory)
    return made


async def test_durable_conflict_is_classified_and_inactive(monkeypatch, caplog):
    spy = SpyService()
    queue = FakeQueue(subscribe_error=Exception("consumer already exists"))
    _install_fake_queue(monkeypatch, queue=queue)
    c = _consumer(spy)
    with caplog.at_level("WARNING"):
        await c.start()  # must not raise
    assert c.active is False
    assert any("durable_conflict" in r.getMessage() for r in caplog.records)
    assert not any("bind_failed" in r.getMessage() for r in caplog.records)


async def test_start_leaves_inactive_on_connect_failure_without_crashing(monkeypatch):
    # A backbone that fails to connect must be swallowed: stay inactive, schedule a
    # background retry, never crash or give up forever.
    spy = SpyService()
    _install_fake_queue(monkeypatch, connect_error=ConnectionRefusedError("backbone down"))
    scheduled = []
    monkeypatch.setattr("consumer.asyncio.create_task", lambda coro: scheduled.append(coro) or coro.close())
    c = _consumer(spy)
    await c.start()
    assert c.active is False
    assert c._queue is None
    assert len(scheduled) == 1  # a retry was scheduled, not abandoned


async def test_retry_connect_recovers_after_repeated_failures(monkeypatch):
    # Drives _retry_connect() directly against a connect() that fails twice before succeeding,
    # confirming the background loop recovers a backbone that was down at startup.
    spy = SpyService()
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

    c = _consumer(spy)
    assert c.active is False
    await c._retry_connect()
    assert calls["n"] == 3
    assert c.active is True


async def test_bind_uses_the_nack_provisioned_consumer(monkeypatch):
    """The binding names the stream + durable and asks for ALWAYS ack (soft-ack); the
    subject/deliver policy/max_deliver live in the NACK consumer, not here."""
    from multistack.queue.drivers.nats.messaging import AckMode

    spy = SpyService()
    queue = FakeQueue()
    _install_fake_queue(monkeypatch, queue=queue)
    c = _consumer(spy)

    await c.start()
    assert c.active is True
    assert queue.subscribe_calls == 1
    binding = queue.bindings[0]
    assert binding.stream == "GATEWAY_EVENTS"
    assert binding.durable == "rpm-counter"
    assert binding.ack_mode is AckMode.ALWAYS


async def test_request_event_is_counted():
    spy = SpyService()
    c = _consumer(spy)
    await c._on_msg({"type": "request", "request_id": "r1", "principal": "u1", "model": "m1", "path": "/v1/chat/completions"})
    assert spy.calls == [("u1", "m1", "/v1/chat/completions", "r1")]


async def test_response_event_is_not_counted():
    spy = SpyService()
    c = _consumer(spy)
    await c._on_msg({"type": "response", "request_id": "r1", "status": 200})
    assert spy.calls == []          # only `request` events feed RPM


async def test_handler_error_is_swallowed_so_a_poison_message_cannot_stall():
    """The SDK acks under AckMode.ALWAYS regardless, but the handler must also never
    propagate — counting errors can't be allowed to take the service down."""

    class BoomService:
        async def count(self, *_args):
            raise RuntimeError("redis down")

    c = RpmConsumer(Settings(event_backbone_url="nats://test"), BoomService())
    await c._on_msg({"type": "request", "request_id": "r1", "principal": "u", "model": "m", "path": "/p"})
