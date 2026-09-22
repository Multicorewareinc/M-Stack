"""The queue capability's spec: type/options contract, wiring, endpoint."""
import pytest

from multistack.queue import NatsQueueOptions, Queue
from multistack.stack import CAPABILITY_OUTPUT, Stack

KUBECONFIG = "/tmp/kc.yaml"


def spec(**kwargs) -> Queue:
    kwargs.setdefault("storage_class", "longhorn")
    return Queue(kubeconfig_path=KUBECONFIG, **kwargs)


def test_there_is_no_ambient_kubeconfig():
    with pytest.raises(ValueError, match="kubeconfig_path is required"):
        Queue(kubeconfig_path="", storage_class="longhorn")


def test_it_declares_cluster_and_storage() -> None:
    assert Queue.REQUIRES == ("cluster", "storage")


def test_from_stack_wires_kubeconfig_and_storage_class() -> None:
    assert Queue.FROM_STACK == {
        "kubeconfig_path": "kubeconfig_path",
        "storage_class": "storage_class",
    }


def test_provides_the_event_backbone_url_key() -> None:
    assert Queue.PROVIDES == {"event_backbone_url": "endpoint"}
    assert CAPABILITY_OUTPUT["queue"] == "event_backbone_url"


def test_nats_is_the_only_supported_type() -> None:
    assert Queue.SUPPORTED_TYPES == ("nats",)
    with pytest.raises(ValueError, match="Unknown queue type"):
        spec(type="rabbitmq")


def test_options_default_to_nats_queue_options() -> None:
    assert isinstance(spec().options, NatsQueueOptions)


def test_an_options_object_for_the_wrong_type_is_rejected() -> None:
    from multistack.cache import ValkeyOptions

    with pytest.raises(ValueError, match="expects options=NatsQueueOptions"):
        Queue(kubeconfig_path=KUBECONFIG, options=ValkeyOptions(),
              type="nats", storage_class="longhorn")


# -- namespace: matches what's actually running, not this implementation's
# own preferred default -- see the spec module's own DEFAULT_NAMESPACE
# comment and examples/full_stack.py's stage_nats docstring.
def test_default_namespace_matches_the_live_manual_deployment() -> None:
    assert Queue.DEFAULT_NAMESPACES == {"nats": "platform"}
    assert spec().resolved_namespace == "platform"


def test_an_explicit_namespace_overrides_the_default() -> None:
    assert spec(namespace="queue").resolved_namespace == "queue"


# -- replicas: JetStream quorum floor ---------------------------------------
def test_replicas_below_two_is_refused() -> None:
    with pytest.raises(ValueError, match="JetStream quorum floor"):
        spec(options=NatsQueueOptions(replicas=1))


def test_replicas_of_two_is_the_floor_not_the_refusal() -> None:
    spec(options=NatsQueueOptions(replicas=2))


# -- endpoint -----------------------------------------------------------
def test_endpoint_is_the_in_cluster_client_url() -> None:
    assert spec().endpoint == "nats://nats.platform.svc.cluster.local:4222"


def test_endpoint_follows_a_renamed_release_and_namespace() -> None:
    q = spec(namespace="queue", options=NatsQueueOptions(release_name="backbone"))
    assert q.endpoint == "nats://backbone.queue.svc.cluster.local:4222"


# -- stack wiring -------------------------------------------------------
def test_stack_fills_in_kubeconfig_and_storage_class() -> None:
    stack = Stack(kubeconfig_path=KUBECONFIG, storage_class="longhorn")
    built = stack.build(Queue)
    assert built.kubeconfig_path == KUBECONFIG
    assert built.storage_class == "longhorn"


def test_recording_a_queue_publishes_the_event_backbone_url() -> None:
    stack = Stack(kubeconfig_path=KUBECONFIG, storage_class="longhorn")
    stack.record(spec())
    assert stack.get("event_backbone_url") == (
        "nats://nats.platform.svc.cluster.local:4222"
    )
