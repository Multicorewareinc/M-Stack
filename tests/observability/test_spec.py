"""Observability spec: validation, naming, and the wiring contract.

The spec's whole job is to be implementation-neutral and to fail before
anything reaches a cluster. These tests are about the second half --
every validator here exists because the value it rejects is one the chart
accepts silently and then misbehaves on.
"""
import pytest

from multistack.observability.spec import (
    KubePrometheusStackOptions,
    Observability,
)
from multistack.stack import CAPABILITY_OUTPUT, Stack

KUBECONFIG = "/tmp/kc.yaml"


def spec(**kwargs) -> Observability:
    return Observability(kubeconfig_path=KUBECONFIG, **kwargs)


# -- construction ---------------------------------------------------------


def test_kubeconfig_is_required_and_never_ambient():
    """No $KUBECONFIG fallback: a spec with no path must fail rather than
    silently target whatever cluster the shell happens to point at."""
    with pytest.raises(ValueError, match="kubeconfig_path is required"):
        Observability(kubeconfig_path="")


def test_options_are_filled_in_without_being_asked_for():
    # The driver reads spec.options.release_name directly, so a None here
    # would be an AttributeError at install time rather than a validation
    # error at construction.
    assert spec().options is not None
    assert spec().options.release_name == "kube-prometheus-stack"


def test_options_belonging_to_another_capability_are_rejected():
    """A subclass of the expected options model is fine -- this is about
    handing over another capability's options object entirely, which
    pydantic would otherwise try to coerce field-by-field."""
    from multistack.policy.spec import RPMOptions

    with pytest.raises(ValueError, match="expects options"):
        Observability(kubeconfig_path=KUBECONFIG, options=RPMOptions(image_tag="0.1.0"))


def test_an_unsupported_type_is_rejected():
    with pytest.raises(ValueError):
        spec(type="victoria_metrics").validate()


# -- validators -----------------------------------------------------------


@pytest.mark.parametrize("value", ["15d", "6h", "4w", "30m", "1y"])
def test_prometheus_durations_are_accepted(value):
    assert spec(metrics_retention=value).metrics_retention == value


@pytest.mark.parametrize("value", ["15", "d", "15 d", "fifteen", ""])
def test_a_retention_that_is_not_a_duration_is_rejected(value):
    """A bare number is not a number of days to Prometheus -- it is
    invalid, and the chart falls back to its own default. Nobody notices
    until the metrics they wanted are already gone."""
    with pytest.raises(ValueError, match="must look like a Prometheus duration"):
        spec(metrics_retention=value)


@pytest.mark.parametrize(
    "field",
    ["prometheus_volume_size", "grafana_volume_size", "alertmanager_volume_size"],
)
def test_a_volume_size_without_a_unit_is_rejected(field):
    """"10" is ten *bytes* to Kubernetes -- a PVC that binds and then
    immediately fills."""
    with pytest.raises(ValueError, match="must be a Kubernetes quantity"):
        spec(**{field: "10"})


@pytest.mark.parametrize("field", ["prometheus_volume_size", "grafana_volume_size"])
def test_volume_sizes_with_units_are_accepted(field):
    assert spec(**{field: "50Gi"})


def test_an_empty_grafana_user_is_rejected():
    with pytest.raises(ValueError, match="grafana_admin_user must not be empty"):
        spec(grafana_admin_user="   ")


def test_an_empty_release_name_is_rejected():
    with pytest.raises(ValueError, match="must not be empty"):
        KubePrometheusStackOptions(release_name="")


# -- naming ---------------------------------------------------------------


def test_the_default_namespace_is_monitoring():
    assert spec().resolved_namespace == "monitoring"


def test_an_explicit_namespace_wins():
    assert spec(namespace="observability").resolved_namespace == "observability"


def test_the_three_endpoints_name_the_chart_s_own_services():
    o = spec()
    assert o.grafana_endpoint() == (
        "http://kube-prometheus-stack-grafana.monitoring.svc.cluster.local"
    )
    # Ports matter: Prometheus and Alertmanager are APIs a consumer calls,
    # Grafana is a UI behind an ingress route.
    assert o.prometheus_endpoint().endswith(":9090")
    assert o.alertmanager_endpoint().endswith(":9093")


def test_renaming_the_release_moves_every_service_name_with_it():
    o = spec(options=KubePrometheusStackOptions(release_name="kube-prometheus-stack-prod"))
    assert "kube-prometheus-stack-prod-grafana" in o.grafana_endpoint()
    assert "kube-prometheus-stack-prod-prometheus" in o.prometheus_endpoint()


def test_set_grafana_credentials_moves_both_together():
    o = spec()
    o.set_grafana_credentials("ops", "s3cret")
    assert (o.grafana_admin_user, o.grafana_admin_password) == ("ops", "s3cret")


def test_a_password_is_not_invented_at_construction():
    """Generating one here would mean a spec that is not reproducible, and
    a password nobody ever sees. The driver fills it in, and only when no
    live release already has one."""
    assert spec().grafana_admin_password is None


# -- the wiring contract --------------------------------------------------


def test_it_requires_a_cluster_and_storage():
    # Storage, not just a cluster: without a StorageClass every sample and
    # every dashboard lives exactly as long as the pod does.
    assert Observability.REQUIRES == ("cluster", "storage")


def test_every_required_capability_has_an_output_key():
    for capability in Observability.REQUIRES:
        assert capability in CAPABILITY_OUTPUT


def test_from_stack_names_real_fields():
    for field in Observability.FROM_STACK:
        assert field in Observability.model_fields


def test_provides_publishes_a_capability_output_key():
    for key, attribute in Observability.PROVIDES.items():
        assert key in CAPABILITY_OUTPUT.values()
        assert hasattr(Observability, attribute)


def test_recording_it_publishes_the_grafana_endpoint():
    # PROVIDES names a method, not a property -- Stack.record has to call
    # it rather than hand back the bound method.
    stack = Stack(KUBECONFIG)
    stack.record(spec())
    assert stack._values["observability_endpoint"] == (
        "http://kube-prometheus-stack-grafana.monitoring.svc.cluster.local"
    )


def test_building_it_without_storage_says_which_layer_is_missing():
    stack = Stack(KUBECONFIG)
    with pytest.raises(Exception, match="requires the 'storage' capability"):
        stack.build(Observability)


def test_building_it_after_storage_fills_both_fields():
    stack = Stack(KUBECONFIG)
    stack.provide(storage_class="longhorn")
    built = stack.build(Observability)
    assert built.kubeconfig_path == KUBECONFIG
    assert built.storage_class == "longhorn"
