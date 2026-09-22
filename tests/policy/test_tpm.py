"""The tpm driver: what it renders, and what it warns about.

Mostly the rpm driver's tests again, which is the point — the two share a
spec and a contract. The tests that are only here cover the places TPM
differs, and each of those is a way to get a limiter that is healthy and
not limiting: a token ceiling that is really a request ceiling, and a
consumer that answers /check while counting nothing.
"""
import pytest

from multistack import Policy
from multistack.policy.drivers.tpm import TPMDriver

KC = "/tmp/kc.yaml"


def policy(**kwargs):
    base = dict(type="tpm", kubeconfig_path=KC,
                cache_url="redis://valkey:6379/0",
                event_backbone_url="nats://nats:4222")
    base.update(kwargs)
    return Policy(**base)


def test_limits_reach_the_chart_structured_not_as_json():
    values = TPMDriver()._values(policy(
        limits={"user_default": 200000,
                "model_overrides": {"limited-model": 50000}}))
    limits = values["secret"]["limits"]
    assert limits["userDefault"] == 200000
    assert limits["modelOverrides"] == {"limited-model": 50000}
    assert isinstance(limits["userOverrides"], dict)


def test_the_counter_store_and_backbone_are_passed_through():
    values = TPMDriver()._values(policy())
    assert values["config"]["valkeyUrl"] == "redis://valkey:6379/0"
    assert values["config"]["events"]["backboneUrl"] == "nats://nats:4222"


def test_no_image_tag_key_when_none_is_pinned():
    assert "image" not in TPMDriver()._values(policy())
    assert TPMDriver()._values(
        policy(options={"image_tag": "0.2.0"}))["image"]["tag"] == "0.2.0"


def test_the_enriched_stream_reaches_the_chart_by_default():
    """TPM subscribes to the enricher's derived stream (ADR-030), not the
    gateway's raw one -- the service itself refuses to start against any
    other pairing, so this is what a caller gets without naming either."""
    values = TPMDriver()._values(policy())
    assert values["config"]["events"]["streamName"] == "GATEWAY_EVENTS_ENRICHED"
    assert values["config"]["events"]["streamSubject"] == "gateway.events.enriched"


def test_the_stream_pairing_can_be_overridden_together():
    values = TPMDriver()._values(policy(options={
        "event_stream_name": "GATEWAY_EVENTS_ENRICHED_V2",
        "event_stream_subject": "gateway.events.enriched.v2",
    }))
    assert values["config"]["events"]["streamName"] == "GATEWAY_EVENTS_ENRICHED_V2"
    assert values["config"]["events"]["streamSubject"] == "gateway.events.enriched.v2"


def test_the_default_durable_is_not_rpms():
    """Both consumers read their own stream independently; the durable
    name is what gives a replica its own cursor if replicaCount changes."""
    assert TPMDriver()._values(
        policy())["config"]["events"]["durableName"] == "tpm-counter-enriched"


def test_rpms_durable_is_refused_at_construction():
    with pytest.raises(ValueError, match="RPM limiter's durable"):
        policy(options={"durable_name": "rpm-counter"})


# -- the warnings, which are the whole point of check_prerequisites -------
def _warnings(p) -> str:
    """check_prerequisites without the cluster checks, which need a real
    kubeconfig. The warnings are computed from the spec alone."""
    driver = TPMDriver()
    collected = []
    if p.allow_no_backbone:
        collected.append("allow_no_backbone")
    # Call the real thing for the limits logic by stubbing the two gates
    # it opens with.
    import multistack.policy.drivers.tpm as mod
    real_cli, real_cluster = mod.require_cli, mod.require_cluster
    mod.require_cli = lambda *a, **k: None
    mod.require_cluster = lambda *a, **k: None
    try:
        return " ".join(driver.check_prerequisites(p))
    finally:
        mod.require_cli, mod.require_cluster = real_cli, real_cluster


def test_a_request_sized_default_is_warned_about():
    """60 is a plausible requests-per-minute ceiling and an impossible
    tokens-per-minute one — a single completion exhausts it. The most
    likely cause is a limits document copied from the rpm limiter."""
    warning = _warnings(policy(limits={"user_default": 60}))
    assert "token counts per minute" in warning
    assert "user_default" in warning


def test_a_realistic_token_default_is_not_warned_about():
    assert "token counts per minute" not in _warnings(
        policy(limits={"user_default": 200000}))


def test_a_small_override_is_left_alone():
    """A deliberately tiny per-model ceiling is a plausible thing to want;
    a tiny default almost never is. Only the defaults are checked."""
    warning = _warnings(policy(limits={"user_default": 200000,
                                       "model_overrides": {"tiny": 10}}))
    assert "token counts per minute" not in warning


def test_all_zero_limits_are_warned_about():
    assert "denies nothing" in _warnings(policy())


def test_enforcing_looks_for_the_keys_the_service_actually_logs():
    """The same check as rpm's, for the same reason: a marker the service
    never logs is a failure enforcing() cannot see."""
    from pathlib import Path

    from multistack.policy.drivers.tpm import INACTIVE_MARKERS

    consumer = (Path(__file__).resolve().parents[2]
                / "api/microservices/rate-limiter-tpm/src/consumer.py").read_text()
    for marker in INACTIVE_MARKERS:
        assert f'"{marker}"' in consumer, (
            f"the driver treats {marker!r} as 'not counting', but the service "
            f"never logs it — so enforcing() cannot see that failure"
        )


def test_cache_auth_url_renders_into_the_secret_not_the_configmap():
    """Same split as the rpm driver. Both limiters read the same cache, so
    a fix that reached only one of them would leave half the chain
    answering NOAUTH."""
    values = TPMDriver()._values(policy(
        cache_auth_url="redis://:s3cret@v:6379/0"))

    assert values["secret"]["valkeyUrl"] == "redis://:s3cret@v:6379/0"
    assert values["config"]["valkeyUrl"] == "redis://valkey:6379/0"
    assert "s3cret" not in str(values["config"])
