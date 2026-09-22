"""The billing capability: spec, wiring, and the stripe driver.

The theme, same as controlplane's: nothing here may carry a credential.
The chart reads DATABASE_URL and SERVICE_API_KEY (and optionally the two
Stripe secrets) from a Kubernetes Secret; this spec carries only that
Secret's name, and the driver tells the chart not to render one of its
own.
"""
import base64
import json

import pytest

from multistack.billing import (
    Billing,
    BillingPrerequisiteError,
    REQUIRED_SECRET_KEYS,
    StripeOptions,
)
from multistack.billing.drivers import stripe as mod
from multistack.stack import CAPABILITY_OUTPUT, Stack

KUBECONFIG = "/tmp/kc.yaml"


def spec(**kwargs) -> Billing:
    kwargs.setdefault("existing_secret", "billing-secrets")
    kwargs.setdefault("event_backbone_url", "nats://nats:4222")
    return Billing(kubeconfig_path=KUBECONFIG, **kwargs)


class _FakeRunner:
    def __init__(self, calls):
        self.calls = calls

    def install_or_upgrade(self, release, **kwargs):
        self.calls.append(("install", release, kwargs))

    def uninstall(self, release, **kwargs):
        self.calls.append(("uninstall", release, kwargs))


def _secret(keys) -> str:
    return json.dumps(
        {"data": {k: base64.b64encode(b"x").decode() for k in keys}}
    )


@pytest.fixture
def driver():
    return mod.StripeBillingDriver()


@pytest.fixture
def calls(driver, monkeypatch):
    """Stubs the outward calls; the Secret exists and is complete."""
    recorded = []
    monkeypatch.setattr(driver, "_helm", lambda b: _FakeRunner(recorded))
    monkeypatch.setattr(mod, "require_cli", lambda *a, **k: None)
    monkeypatch.setattr(mod, "require_cluster", lambda *a, **k: None)
    monkeypatch.setattr(
        mod, "kubectl",
        lambda *a, **k: _secret((*REQUIRED_SECRET_KEYS,
                                 "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET")),
    )
    return recorded


def _install(calls):
    return next(c[2] for c in calls if c[0] == "install")


# -- spec ---------------------------------------------------------------
def test_the_secret_name_is_required():
    with pytest.raises(ValueError, match="existing_secret is required"):
        Billing(kubeconfig_path=KUBECONFIG, existing_secret="",
                event_backbone_url="nats://x")


def test_no_credential_field_exists_on_the_spec():
    """The guard that matters: a spec is a file people commit, so there
    must be nowhere in it for a password to go."""
    forbidden = {
        "database_url", "service_api_key", "stripe_secret_key",
        "stripe_webhook_secret", "password",
    }
    assert not (forbidden & set(Billing.model_fields))


def test_there_is_no_ambient_kubeconfig():
    with pytest.raises(ValueError, match="kubeconfig_path is required"):
        Billing(kubeconfig_path="", existing_secret="s",
                event_backbone_url="nats://x")


def test_an_empty_backbone_is_refused_unless_deliberate():
    with pytest.raises(ValueError, match="nothing would be metered"):
        Billing(kubeconfig_path=KUBECONFIG, existing_secret="s")
    # Deliberate is fine.
    Billing(kubeconfig_path=KUBECONFIG, existing_secret="s",
            allow_no_backbone=True)


def test_it_declares_cluster_and_database():
    """A database it owns exclusively, and no cache -- billing has no
    VALKEY_URL/REDIS_URL in its settings at all."""
    assert Billing.REQUIRES == ("cluster", "database")


def test_it_publishes_the_key_a_consumer_would_read():
    assert Billing.PROVIDES == {"billing_endpoint": "endpoint"}
    assert CAPABILITY_OUTPUT["billing"] == "billing_endpoint"


def test_stripe_is_the_only_implementation():
    assert Billing.SUPPORTED_TYPES == ("stripe",)
    assert spec().type == "stripe"


def test_a_backoff_base_greater_than_its_own_max_is_refused():
    with pytest.raises(ValueError, match="reporter_backoff_base_seconds"):
        spec(options=StripeOptions(
            reporter_backoff_base_seconds=100, reporter_backoff_max_seconds=10))


def test_the_endpoint_carries_no_credential():
    assert spec().endpoint == (
        "http://billing.billing.svc.cluster.local:8000"
    )


def test_recording_it_publishes_billing_endpoint():
    stack = Stack(kubeconfig_path=KUBECONFIG)
    stack.record(spec())
    assert stack.outputs["billing_endpoint"] == spec().endpoint


# -- driver ---------------------------------------------------------------
def test_the_backbone_and_options_reach_the_chart(driver):
    values = driver._values(spec(options=StripeOptions(
        durable_name="custom-durable",
        stripe_meter_event_name="my_tokens",
        reporter_batch_size=25,
    )))
    events = values["config"]["events"]
    assert events["backboneUrl"] == "nats://nats:4222"
    assert events["durableName"] == "custom-durable"
    assert values["config"]["stripe"]["meterEventName"] == "my_tokens"
    assert values["config"]["reporter"]["batchSize"] == 25


def test_the_secret_is_named_never_created(driver):
    values = driver._values(spec(existing_secret="my-billing-secrets"))
    assert values["secret"] == {
        "existingSecret": "my-billing-secrets", "create": False}


def test_no_image_tag_key_when_none_is_pinned(driver):
    assert "image" not in driver._values(spec())
    assert driver._values(
        spec(options=StripeOptions(image_tag="0.2.0")))["image"]["tag"] == "0.2.0"


def test_a_missing_secret_is_refused_before_install(driver, monkeypatch):
    monkeypatch.setattr(mod, "require_cli", lambda *a, **k: None)
    monkeypatch.setattr(mod, "require_cluster", lambda *a, **k: None)
    monkeypatch.setattr(mod, "kubectl", lambda *a, **k: "")

    with pytest.raises(BillingPrerequisiteError, match="does not exist"):
        driver.check_prerequisites(spec())


def test_a_secret_missing_required_keys_is_refused(driver, monkeypatch):
    monkeypatch.setattr(mod, "require_cli", lambda *a, **k: None)
    monkeypatch.setattr(mod, "require_cluster", lambda *a, **k: None)
    monkeypatch.setattr(mod, "kubectl", lambda *a, **k: _secret(["DATABASE_URL"]))

    with pytest.raises(BillingPrerequisiteError, match="SERVICE_API_KEY"):
        driver.check_prerequisites(spec())


def test_missing_stripe_keys_are_warned_about_not_refused(driver, monkeypatch):
    monkeypatch.setattr(mod, "require_cli", lambda *a, **k: None)
    monkeypatch.setattr(mod, "require_cluster", lambda *a, **k: None)
    monkeypatch.setattr(mod, "kubectl", lambda *a, **k: _secret(REQUIRED_SECRET_KEYS))

    warnings = " ".join(driver.check_prerequisites(spec()))
    assert "STRIPE_SECRET_KEY" in warnings
    assert "STRIPE_WEBHOOK_SECRET" in warnings


def test_both_stripe_keys_present_warns_about_neither(driver, calls):
    assert driver.check_prerequisites(spec()) == []


def test_create_installs_and_returns_the_endpoint(driver, calls):
    endpoint = driver.create(spec())
    installed = _install(calls)
    assert installed["namespace"] == "billing"
    assert endpoint == spec().endpoint


def test_delete_uninstalls_by_release_name_and_leaves_the_database(driver, calls):
    driver.delete(spec())
    assert calls[-1] == (
        "uninstall", "billing",
        {"namespace": "billing", "missing_ok": True},
    )
