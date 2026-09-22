"""NatsStreamManager: idempotent creation of the platform's two JetStream
streams (docs/nats.md) through the admin client's `nats` CLI."""
import json

from multistack.nats.deployment import NatsDeployment, StreamConfig
from multistack.nats.deployment.client import NatsAdminClient
from multistack.nats.deployment.streams import DEFAULT_STREAMS, NatsStreamManager

KC = "/tmp/kc.yaml"


def deployment(**kwargs):
    base = dict(kubeconfig_path=KC, namespace="nats")
    base.update(kwargs)
    return NatsDeployment(**base)


class FakeAdminClient(NatsAdminClient):
    """Records every `nats` invocation without touching kubectl at all."""

    def __init__(self, *, existing=()):
        self.existing = list(existing)
        self.calls = []
        self.ensured = 0

    def ensure(self, deployment, **kw):
        self.ensured += 1

    def exec(self, deployment, *args, **kw):
        self.calls.append(args)
        if args[:2] == ("stream", "ls"):
            return json.dumps(self.existing)
        return ""


def test_ensure_creates_both_default_streams_on_a_clean_cluster():
    admin = FakeAdminClient(existing=[])
    NatsStreamManager(admin).ensure(deployment())
    created = [c[2] for c in admin.calls if c[:2] == ("stream", "add")]
    assert created == ["GATEWAY_EVENTS", "GATEWAY_EVENTS_ENRICHED"]


def test_ensure_skips_a_stream_that_already_exists():
    admin = FakeAdminClient(existing=["GATEWAY_EVENTS"])
    NatsStreamManager(admin).ensure(deployment())
    created = [c[2] for c in admin.calls if c[:2] == ("stream", "add")]
    assert created == ["GATEWAY_EVENTS_ENRICHED"]


def test_ensure_is_a_full_no_op_when_both_streams_exist():
    admin = FakeAdminClient(existing=["GATEWAY_EVENTS", "GATEWAY_EVENTS_ENRICHED"])
    NatsStreamManager(admin).ensure(deployment())
    assert [c for c in admin.calls if c[:2] == ("stream", "add")] == []


def test_create_carries_the_configured_subject_and_retention():
    admin = FakeAdminClient(existing=[])
    NatsStreamManager(admin).ensure(
        deployment(),
        streams=[StreamConfig(name="X", subject="x.y", max_age="1h")],
    )
    add_call = [c for c in admin.calls if c[:2] == ("stream", "add")][0]
    assert "--subjects=x.y" in add_call
    assert "--max-age=1h" in add_call


def test_default_streams_match_the_raw_enriched_split():
    names = {s.name: s for s in DEFAULT_STREAMS}
    assert names["GATEWAY_EVENTS"].subject == "gateway.events"
    assert names["GATEWAY_EVENTS_ENRICHED"].subject == "gateway.events.enriched"
    # The enriched stream is what billing bills off of -- it keeps a
    # longer window (72h) than the raw input it was built from (24h).
    assert names["GATEWAY_EVENTS"].max_age == "24h"
    assert names["GATEWAY_EVENTS_ENRICHED"].max_age == "72h"
