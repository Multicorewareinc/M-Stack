"""The enricher capability: spec, wiring, and the JetStream driver."""

import pytest

from multistack.enricher import Enricher, EnricherOptions
from multistack.enricher.drivers import enricher as mod
from multistack.stack import CAPABILITY_OUTPUT, Stack

KUBECONFIG = "/tmp/kc.yaml"


def spec(**kwargs) -> Enricher:
    kwargs.setdefault("event_backbone_url", "nats://nats:4222")
    return Enricher(kubeconfig_path=KUBECONFIG, **kwargs)


class _FakeRunner:
    def __init__(self, calls):
        self.calls = calls

    def install_or_upgrade(self, release, **kwargs):
        self.calls.append(("install", release, kwargs))

    def uninstall(self, release, **kwargs):
        self.calls.append(("uninstall", release, kwargs))


@pytest.fixture
def driver():
    return mod.JetStreamEnricherDriver()


@pytest.fixture
def calls(driver, monkeypatch):
    recorded = []
    monkeypatch.setattr(driver, "_helm", lambda e: _FakeRunner(recorded))
    monkeypatch.setattr(mod, "require_cli", lambda *a, **k: None)
    monkeypatch.setattr(mod, "require_cluster", lambda *a, **k: None)
    return recorded


def _install(calls):
    return next(c[2] for c in calls if c[0] == "install")


# -- spec -------------------------------------------------------------------
def test_there_is_no_ambient_kubeconfig():
    with pytest.raises(ValueError, match="kubeconfig_path is required"):
        Enricher(kubeconfig_path="", event_backbone_url="nats://x")


def test_an_empty_backbone_is_refused_unless_deliberate():
    with pytest.raises(ValueError, match="nothing would be enriched"):
        Enricher(kubeconfig_path=KUBECONFIG)
    # Deliberate is fine.
    Enricher(kubeconfig_path=KUBECONFIG, allow_no_backbone=True)


def test_it_declares_the_cluster_dependency_only():
    assert Enricher.REQUIRES == ("cluster",)


def test_it_has_no_provides():
    """Nothing downstream reaches this over HTTP -- TPM and billing
    consume its output through NATS stream/subject strings, which are
    plain configuration rather than something worth publishing."""
    assert not getattr(Enricher, "PROVIDES", {})
    assert "enricher" not in CAPABILITY_OUTPUT


def test_the_default_streams_are_the_ones_the_chart_expects():
    options = spec().options
    assert options.event_stream_name == "GATEWAY_EVENTS"
    assert options.event_stream_subject == "gateway.events"
    assert options.enriched_stream_name == "GATEWAY_EVENTS_ENRICHED"
    assert options.enriched_subject == "gateway.events.enriched"


def test_ack_wait_must_cover_a_tokenizer_call_plus_margin():
    """Mirrors the chart's own configmap.yaml `fail()` -- caught here at
    construction rather than at `helm install`."""
    with pytest.raises(ValueError, match="ack_wait_seconds"):
        spec(options=EnricherOptions(tokenizer_url="http://tok:8000", ack_wait_seconds=1))
    # 2000ms timeout + 5000ms margin = 7000ms -> 7s is the floor.
    spec(options=EnricherOptions(tokenizer_url="http://tok:8000", ack_wait_seconds=7))


def test_no_tokenizer_url_skips_the_ack_wait_check():
    spec(options=EnricherOptions(ack_wait_seconds=1))


def test_the_endpoint_is_for_health_checks_only():
    assert spec().endpoint == ("http://enricher.policy.svc.cluster.local:8000")


def test_recording_it_satisfies_nothing_downstream():
    stack = Stack(kubeconfig_path=KUBECONFIG)
    before = dict(stack.outputs)
    stack.record(spec())
    assert stack.outputs == before


# -- driver -------------------------------------------------------------
def test_the_backbone_and_streams_reach_the_chart(driver):
    values = driver._values(
        spec(
            options=EnricherOptions(
                event_stream_name="X",
                event_stream_subject="x.y",
                enriched_stream_name="XE",
                enriched_subject="x.y.enriched",
            )
        )
    )
    events = values["config"]["events"]
    assert events["backboneUrl"] == "nats://nats:4222"
    assert events["streamName"] == "X"
    assert events["streamSubject"] == "x.y"
    assert events["enrichedStreamName"] == "XE"
    assert events["enrichedSubject"] == "x.y.enriched"


def test_the_tokenizer_fallback_reaches_the_chart(driver):
    values = driver._values(
        spec(
            options=EnricherOptions(
                tokenizer_url="http://tok:8000/tokenize",
                tokenizer_timeout_ms=500,
            )
        )
    )
    assert values["config"]["tokenizer"] == {"url": "http://tok:8000/tokenize", "timeoutMs": 500}


def test_no_image_tag_key_when_none_is_pinned(driver):
    assert "image" not in driver._values(spec())
    assert (
        driver._values(spec(options=EnricherOptions(image_tag="0.2.0")))["image"]["tag"] == "0.2.0"
    )


def test_allow_no_backbone_is_warned_about(driver, calls):
    warnings = " ".join(
        driver.check_prerequisites(spec(allow_no_backbone=True, event_backbone_url=""))
    )
    assert "allow_no_backbone" in warnings


def test_an_absent_tokenizer_is_warned_about(driver, calls):
    assert "tokenizer_url is empty" in " ".join(driver.check_prerequisites(spec()))
    assert "tokenizer_url is empty" not in " ".join(
        driver.check_prerequisites(spec(options=EnricherOptions(tokenizer_url="http://tok:8000")))
    )


def test_create_installs_and_returns_the_endpoint(driver, calls):
    endpoint = driver.create(spec())
    installed = _install(calls)
    assert installed["namespace"] == "policy"
    assert endpoint == spec().endpoint


def test_delete_uninstalls_by_release_name(driver, calls):
    driver.delete(spec())
    assert calls[-1] == (
        "uninstall",
        "enricher",
        {"namespace": "policy", "missing_ok": True},
    )


# -- affinity ------------------------------------------------------------
# The chart excludes the control plane by default. On a single-node cluster
# the only node IS the control plane, so the release installs cleanly and
# the pod stays Pending; {} overrides that exclusion deliberately.


def test_an_empty_affinity_is_sent_as_null_not_an_empty_map(driver, calls):
    """Helm merges maps recursively, so {} leaves the chart's own affinity in
    place and the pod stays Pending; null is what deletes the key."""
    driver.create(spec(affinity={}))
    values = _install(calls)["values"]
    assert "affinity" in values and values["affinity"] is None


def test_no_affinity_means_the_key_is_absent(driver, calls):
    driver.create(spec())
    assert "affinity" not in _install(calls)["values"]
