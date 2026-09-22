"""The queue capability's JetStream driver: translates a Queue spec into a
NatsDeployment and delegates to NatsDeploymentManager -- never reimplements
the Helm/kubectl mechanics that package already owns."""
import pytest

from multistack.nats.deployment import NatsDeploymentInfo
from multistack.queue.base import QueueError
from multistack.queue.drivers import nats as mod
from multistack.queue.spec import NatsQueueOptions, Queue

KUBECONFIG = "/tmp/kc.yaml"


def spec(**kwargs) -> Queue:
    kwargs.setdefault("storage_class", "longhorn")
    return Queue(kubeconfig_path=KUBECONFIG, **kwargs)


class FakeStreams:
    def __init__(self, names=()):
        self.names = list(names)
        self.calls = []

    def list(self, deployment):
        self.calls.append(("list", deployment))
        return self.names


class FakeManager:
    def __init__(self):
        self.calls = []
        self.streams = FakeStreams(["GATEWAY_EVENTS", "GATEWAY_EVENTS_ENRICHED"])
        self._deployed = False

    def create(self, deployment):
        self.calls.append(("create", deployment))
        return NatsDeploymentInfo(
            release_name=deployment.release_name,
            namespace=deployment.namespace,
            status="deployed",
            client_url=(
                f"nats://{deployment.release_name}."
                f"{deployment.namespace}.svc.cluster.local:4222"
            ),
            monitor_url="http://nats.platform.svc.cluster.local:8222",
            metrics_url="http://nats.platform.svc.cluster.local:7777",
        )

    def ensure_streams(self, deployment):
        self.calls.append(("ensure_streams", deployment))

    def delete(self, deployment):
        self.calls.append(("delete", deployment))

    def is_deployed(self, deployment):
        self.calls.append(("is_deployed", deployment))
        return self._deployed

    def list_consumers(self, deployment, stream):
        self.calls.append(("list_consumers", deployment, stream))
        return ["rpm-counter"]

    def consumer_info(self, deployment, stream, consumer):
        self.calls.append(("consumer_info", deployment, stream, consumer))
        return {"name": consumer}


@pytest.fixture
def driver():
    d = mod.JetStreamQueueDriver()
    d._manager = FakeManager()
    return d


# -- translation --------------------------------------------------------
def test_deployment_carries_the_spec_fields_across() -> None:
    driver = mod.JetStreamQueueDriver()
    q = spec(namespace="queue", options=NatsQueueOptions(
        release_name="backbone", replicas=5, pvc_size="50Gi",
    ))
    deployment = driver._deployment(q)
    assert deployment.kubeconfig_path == KUBECONFIG
    assert deployment.release_name == "backbone"
    assert deployment.namespace == "queue"
    assert deployment.replicas == 5
    assert deployment.pvc_size == "50Gi"
    assert deployment.storage_class == "longhorn"


def test_an_unset_storage_class_falls_back_to_longhorn() -> None:
    driver = mod.JetStreamQueueDriver()
    q = Queue(kubeconfig_path=KUBECONFIG)
    deployment = driver._deployment(q)
    assert deployment.storage_class == "longhorn"


# -- lifecycle ------------------------------------------------------------
def test_create_installs_and_ensures_the_streams(driver) -> None:
    url = driver.create(spec())
    kinds = [c[0] for c in driver._manager.calls]
    assert kinds == ["create", "ensure_streams"]
    assert url == "nats://nats.platform.svc.cluster.local:4222"


def test_update_takes_the_same_path_as_create(driver) -> None:
    driver.update(spec())
    assert [c[0] for c in driver._manager.calls] == ["create", "ensure_streams"]


def test_delete_uninstalls_the_release(driver) -> None:
    driver.delete(spec())
    assert driver._manager.calls == [
        ("delete", driver._manager.calls[0][1])
    ]


def test_a_manager_failure_becomes_a_queue_error(driver) -> None:
    def boom(deployment):
        raise RuntimeError("helm upgrade timed out")

    driver._manager.create = boom
    with pytest.raises(QueueError, match="helm upgrade timed out"):
        driver.create(spec())


# -- inspection -------------------------------------------------------------
def test_list_streams_delegates_to_the_manager(driver) -> None:
    assert driver.list_streams(spec()) == [
        "GATEWAY_EVENTS", "GATEWAY_EVENTS_ENRICHED",
    ]


def test_list_consumers_delegates_to_the_manager(driver) -> None:
    assert driver.list_consumers(spec(), "GATEWAY_EVENTS") == ["rpm-counter"]


def test_exists_delegates_to_is_deployed(driver) -> None:
    driver._manager._deployed = True
    assert driver.exists(spec()) is True


# -- prerequisites ------------------------------------------------------
def test_check_prerequisites_checks_helm_and_the_cluster(monkeypatch) -> None:
    driver = mod.JetStreamQueueDriver()
    calls = []
    monkeypatch.setattr(mod, "require_cli", lambda *a, **k: calls.append(("cli", a, k)))
    monkeypatch.setattr(mod, "require_cluster", lambda *a, **k: calls.append(("cluster", a, k)))
    warnings = driver.check_prerequisites(spec())
    assert warnings == []
    assert [c[0] for c in calls] == ["cli", "cluster"]
