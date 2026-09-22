"""NatsConsumerInspector: read-only visibility into the durable push
consumers the platform's services bind themselves -- this package never
creates one (see the module's own docstring for why)."""
import json

from multistack.nats.deployment import NatsDeployment
from multistack.nats.deployment.client import NatsAdminClient
from multistack.nats.deployment.consumers import NatsConsumerInspector

KC = "/tmp/kc.yaml"


def deployment(**kwargs):
    base = dict(kubeconfig_path=KC, namespace="nats")
    base.update(kwargs)
    return NatsDeployment(**base)


class FakeAdminClient(NatsAdminClient):
    def __init__(self, *, names=(), info_payload=None):
        self.names = list(names)
        self.info_payload = info_payload or {}
        self.ensured = 0

    def ensure(self, deployment, **kw):
        self.ensured += 1

    def exec(self, deployment, *args, **kw):
        if args[:2] == ("consumer", "ls"):
            return json.dumps(self.names)
        if args[:2] == ("consumer", "info"):
            return json.dumps(self.info_payload)
        return ""


def test_list_returns_the_bound_consumer_names():
    admin = FakeAdminClient(names=["rpm-counter", "enricher"])
    result = NatsConsumerInspector(admin).list(deployment(), "GATEWAY_EVENTS")
    assert result == ["rpm-counter", "enricher"]
    assert admin.ensured == 1


def test_list_on_an_empty_stream_is_an_empty_list():
    admin = FakeAdminClient(names=[])
    assert NatsConsumerInspector(admin).list(deployment(), "GATEWAY_EVENTS") == []


def test_info_parses_delivery_and_ack_state():
    admin = FakeAdminClient(info_payload={
        "config": {"filter_subject": "gateway.events"},
        "num_pending": 0,
        "num_ack_pending": 0,
        "num_redelivered": 0,
        "delivered": {"stream_seq": 371, "consumer_seq": 371},
    })
    info = NatsConsumerInspector(admin).info(deployment(), "GATEWAY_EVENTS", "rpm-counter")
    assert info.name == "rpm-counter"
    assert info.stream == "GATEWAY_EVENTS"
    assert info.filter_subject == "gateway.events"
    assert info.num_ack_pending == 0
    assert info.delivered_stream_seq == 371
    assert info.delivered_consumer_seq == 371


def test_info_defaults_missing_fields_to_zero_rather_than_raising():
    admin = FakeAdminClient(info_payload={})
    info = NatsConsumerInspector(admin).info(deployment(), "GATEWAY_EVENTS", "rpm-counter")
    assert info.num_pending == 0
    assert info.num_ack_pending == 0
    assert info.filter_subject is None
