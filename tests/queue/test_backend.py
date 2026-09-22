"""QueueBackend: dispatch to the nats driver, and the state-tracking
decorators it carries. Driven through the backend rather than the driver
directly, the same reasoning tests/cache/test_valkey.py gives: the driver
itself does no recording, only the backend's own decorated methods do.
"""
import pytest

import multistack.state.tracking as tracking
from multistack.queue import Queue, QueueBackend
from multistack.queue.drivers.nats import JetStreamQueueDriver

KUBECONFIG = "/tmp/kc.yaml"


def spec(**kwargs) -> Queue:
    kwargs.setdefault("storage_class", "longhorn")
    return Queue(kubeconfig_path=KUBECONFIG, **kwargs)


class FakeManager:
    def __init__(self):
        self.calls = []

    def create(self, deployment):
        self.calls.append(("create", deployment))

        class Info:
            client_url = "nats://nats.platform.svc.cluster.local:4222"

        return Info()

    def ensure_streams(self, deployment):
        self.calls.append(("ensure_streams", deployment))

    def delete(self, deployment):
        self.calls.append(("delete", deployment))

    def is_deployed(self, deployment):
        return True

    streams = type("S", (), {"list": lambda self, d: ["GATEWAY_EVENTS"]})()

    def list_consumers(self, deployment, stream):
        return ["rpm-counter"]

    def consumer_info(self, deployment, stream, consumer):
        return {"name": consumer}


@pytest.fixture
def backend(monkeypatch):
    # Queue declares REQUIRES = ("cluster", "storage"), and @track_create
    # refuses to run until the state layer holds a healthy row for each --
    # same seeding tests/cache/test_valkey.py does for "cluster" alone.
    state = tracking.default_state_manager()
    state.start("test-cluster", component_type="cluster")
    state.mark_healthy("test-cluster")
    state.start("test-storage", component_type="storage")
    state.mark_healthy("test-storage")

    monkeypatch.setattr(
        "multistack.capability.require_cluster",
        lambda path, capability="?", **k: "stubbed",
    )
    monkeypatch.setattr(
        "multistack.capability.require_storage_class",
        lambda path, name=None, capability="?", **k: name or "longhorn",
    )
    monkeypatch.setattr(JetStreamQueueDriver, "__init__",
                         lambda self: setattr(self, "_manager", FakeManager()))
    return QueueBackend()


def test_create_is_recorded_as_the_queue_capability(backend):
    from multistack.state.tracking import default_state_manager

    backend.create(spec())

    state = default_state_manager()
    row = state.get("nats")
    assert row is not None and row.component_type == "queue"


def test_delete_removes_the_row(backend):
    backend.create(spec())
    backend.delete(spec())

    from multistack.state.tracking import default_state_manager
    assert default_state_manager().get("nats") is None


def test_update_is_recorded_too(backend):
    backend.create(spec())
    backend.update(spec())

    from multistack.state.tracking import default_state_manager
    row = default_state_manager().get("nats")
    assert row is not None and row.component_type == "queue"


def test_the_backend_reaches_the_streams_and_consumers_through_the_driver(backend):
    backend.create(spec())
    assert backend.list_streams(spec()) == ["GATEWAY_EVENTS"]
    assert backend.list_consumers(spec(), "GATEWAY_EVENTS") == ["rpm-counter"]
    assert backend.consumer_info(spec(), "GATEWAY_EVENTS", "rpm-counter") == {
        "name": "rpm-counter"
    }
