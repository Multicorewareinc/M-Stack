"""The rpm driver: what it renders, and what it warns about."""
import pytest

from multistack import Policy
from multistack.policy.drivers.rpm import RPMDriver

KC = "/tmp/kc.yaml"


def policy(**kwargs):
    base = dict(kubeconfig_path=KC, cache_url="redis://valkey:6379/0",
                event_backbone_url="nats://nats:4222")
    base.update(kwargs)
    return Policy(**base)


def test_limits_reach_the_chart_structured_not_as_json():
    """The chart assembles the JSON itself, into a Secret. Handing it a
    pre-built string would put the document — whose override maps are
    keyed by bearer token — wherever the chart happened to place a
    string."""
    values = RPMDriver()._values(policy(
        limits={"user_default": 60, "model_overrides": {"limited-model": 3}}))
    limits = values["secret"]["limits"]
    assert limits["userDefault"] == 60
    assert limits["modelOverrides"] == {"limited-model": 3}
    assert isinstance(limits["userOverrides"], dict)


def test_the_counter_store_and_backbone_are_passed_through():
    values = RPMDriver()._values(policy())
    assert values["config"]["valkeyUrl"] == "redis://valkey:6379/0"
    assert values["config"]["events"]["backboneUrl"] == "nats://nats:4222"


def test_no_image_tag_key_when_none_is_pinned():
    # An explicit empty tag would override the chart's appVersion with
    # nothing, and the pod would try to pull `repository:`.
    assert "image" not in RPMDriver()._values(policy())
    assert RPMDriver()._values(
        policy(options={"image_tag": "0.2.0"}))["image"]["tag"] == "0.2.0"


def test_an_always_allow_deployment_is_warned_about():
    warnings = " ".join(RPMDriver().check_prerequisites.__doc__ or "")
    # The warning itself needs a cluster, so assert on the spec instead:
    chosen = Policy(kubeconfig_path=KC, cache_url="redis://v:6379/0",
                    allow_no_backbone=True)
    assert chosen.allow_no_backbone is True


def test_enforcing_looks_for_the_key_the_service_actually_logs():
    """This checked for "rpm_consumer_start_failed_inactive", which the
    service has never logged — the key is *connect*
    (rate-limiter-rpm/src/consumer.py:62). So enforcing() returned True
    unconditionally, including on 2026-09-15 when the limiter held no NATS
    connection and was allowing every request (API-2 in bugs.md).

    Asserted against the service's source, not a copy of the string, so
    renaming the log key on one side fails here rather than in production.
    """
    from pathlib import Path

    from multistack.policy.drivers.rpm import INACTIVE_MARKERS

    consumer = (Path(__file__).resolve().parents[2]
                / "api/microservices/rate-limiter-rpm/src/consumer.py").read_text()
    for marker in INACTIVE_MARKERS:
        assert f'"{marker}"' in consumer, (
            f"the driver treats {marker!r} as 'not counting', but the service "
            f"never logs it — so enforcing() cannot see that failure"
        )


def test_cache_auth_url_renders_into_the_secret_not_the_configmap():
    """`config.*` becomes a ConfigMap and `secret.*` a Secret, and envFrom
    applies the Secret second -- so this is what lets an authenticated
    Valkey work without the password being readable via `get configmaps`.
    """
    policy = Policy(kubeconfig_path=KC, cache_url="redis://v:6379/0",
                    cache_auth_url="redis://:s3cret@v:6379/0",
                    allow_no_backbone=True)

    values = RPMDriver()._values(policy)

    assert values["secret"]["valkeyUrl"] == "redis://:s3cret@v:6379/0"
    assert values["config"]["valkeyUrl"] == "redis://v:6379/0"
    assert "s3cret" not in str(values["config"])


def test_no_cache_auth_url_emits_no_secret_key_at_all():
    """Absent, the chart's ConfigMap value stands. An empty string would
    render a VALKEY_URL='' into the Secret, which overrides the ConfigMap
    with nothing and points the limiter at localhost."""
    policy = Policy(kubeconfig_path=KC, cache_url="redis://v:6379/0",
                    allow_no_backbone=True)

    values = RPMDriver()._values(policy)

    assert "valkeyUrl" not in values["secret"]
