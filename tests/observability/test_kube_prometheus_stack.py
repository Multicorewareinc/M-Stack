"""The kube-prometheus-stack driver.

Two seams are stubbed: `_helm()`, which returns the HelmRunner every
install goes through, and the module-level `kubectl`/`require_cli`/
`require_cluster` helpers the driver imported by name. Nothing here
reaches a cluster or the network -- the Longhorn port taught us what it
costs when a test thinks it has stubbed the outward call and has not.
"""
import base64
import json

import pytest

from multistack.observability.base import (
    ObservabilityError,
    ObservabilityPrerequisiteError,
)
from multistack.observability.drivers import kube_prometheus_stack as kps
from multistack.observability.spec import (
    KubePrometheusStackOptions,
    Observability,
)

KUBECONFIG = "/tmp/kc.yaml"


def spec(**kwargs) -> Observability:
    return Observability(kubeconfig_path=KUBECONFIG, **kwargs)


class _FakeRunner:
    def __init__(self, calls):
        self.calls = calls

    def install_or_upgrade(self, release, **kwargs):
        self.calls.append(("install", release, kwargs))
        return {"release": release}

    def uninstall(self, release, **kwargs):
        self.calls.append(("uninstall", release, kwargs))


@pytest.fixture
def driver():
    return kps.KubePrometheusStackDriver()


@pytest.fixture
def calls(driver, monkeypatch):
    """Stubs every outward call and returns the recording list."""
    recorded = []
    monkeypatch.setattr(driver, "_helm", lambda o: _FakeRunner(recorded))
    monkeypatch.setattr(kps, "require_cli", lambda *a, **k: None)
    monkeypatch.setattr(kps, "require_cluster", lambda *a, **k: None)
    # No live release by default: the Secret lookup returns nothing.
    monkeypatch.setattr(
        kps, "kubectl",
        lambda *a, **k: recorded.append(("kubectl", a)) or "",
    )
    return recorded


def _install(calls) -> dict:
    return next(c[2] for c in calls if c[0] == "install")


def _values(calls) -> dict:
    return _install(calls)["values"]


def _secret_json(user: str, password: str) -> str:
    b64 = lambda v: base64.b64encode(v.encode()).decode()
    return json.dumps({"data": {"admin-user": b64(user), "admin-password": b64(password)}})


# -- create ---------------------------------------------------------------


def test_create_returns_the_grafana_endpoint(driver, calls):
    assert driver.create(spec()) == (
        "http://kube-prometheus-stack-grafana.monitoring.svc.cluster.local"
    )


def test_create_installs_the_chart_into_the_resolved_namespace(driver, calls):
    driver.create(spec())
    _, release, kwargs = next(c for c in calls if c[0] == "install")
    assert release == "kube-prometheus-stack"
    assert kwargs["chart"] == "kube-prometheus-stack"
    assert kwargs["namespace"] == "monitoring"


def test_create_maps_retention_onto_the_chart_s_own_spelling(driver, calls):
    driver.create(spec(metrics_retention="30d"))
    assert _values(calls)["prometheus"]["prometheusSpec"]["retention"] == "30d"


def test_create_maps_every_volume_size(driver, calls):
    driver.create(
        spec(
            prometheus_volume_size="50Gi",
            grafana_volume_size="10Gi",
            alertmanager_volume_size="3Gi",
        )
    )
    v = _values(calls)
    prom = v["prometheus"]["prometheusSpec"]["storageSpec"]["volumeClaimTemplate"]
    alert = v["alertmanager"]["alertmanagerSpec"]["storage"]["volumeClaimTemplate"]
    assert prom["spec"]["resources"]["requests"]["storage"] == "50Gi"
    assert alert["spec"]["resources"]["requests"]["storage"] == "3Gi"
    assert v["grafana"]["persistence"]["size"] == "10Gi"


def test_a_named_storage_class_reaches_every_claim(driver, calls):
    driver.create(spec(storage_class="longhorn"))
    v = _values(calls)
    prom = v["prometheus"]["prometheusSpec"]["storageSpec"]["volumeClaimTemplate"]
    assert prom["spec"]["storageClassName"] == "longhorn"
    assert v["grafana"]["persistence"]["storageClassName"] == "longhorn"


def test_no_storage_class_means_the_key_is_absent_not_null(driver, calls):
    """A null storageClassName is not "use the default" to Kubernetes --
    it is a request for a PVC with no class, which on some clusters never
    binds. Omitting the key is what actually means "use the default"."""
    driver.create(spec(storage_class=None))
    v = _values(calls)
    prom = v["prometheus"]["prometheusSpec"]["storageSpec"]["volumeClaimTemplate"]
    assert "storageClassName" not in prom["spec"]
    assert "storageClassName" not in v["grafana"]["persistence"]


def test_grafana_persistence_is_on_by_default(driver, calls):
    # Off, a Grafana reschedule loses every dashboard anyone built.
    driver.create(spec())
    assert _values(calls)["grafana"]["persistence"]["enabled"] is True


def test_the_grafana_service_type_comes_from_options(driver, calls):
    driver.create(
        spec(options=KubePrometheusStackOptions(grafana_service_type="LoadBalancer"))
    )
    assert _values(calls)["grafana"]["service"]["type"] == "LoadBalancer"


def test_extra_values_override_what_is_modelled(driver, calls):
    driver.create(spec(extra_values={"grafana": {"replicas": 2}}))
    # Shallow merge, documented as such: the whole grafana block is
    # replaced rather than merged into.
    assert _values(calls)["grafana"] == {"replicas": 2}


def test_create_pins_the_chart_version_when_given(driver, calls):
    driver.create(spec(chart_version="65.1.0"))
    assert _install(calls)["chart_version"] == "65.1.0"


def test_create_turns_off_strict_value_validation(driver, calls):
    """Grafana's chart documents adminPassword as a comment rather than a
    real default, so it is absent from the parsed defaults the validator
    checks against even though the chart reads it. Strict mode would
    reject every install."""
    driver.create(spec())
    assert _install(calls)["strict_values"] is False


# -- Grafana credentials --------------------------------------------------


def test_a_password_is_generated_when_there_is_no_live_release(driver, calls):
    o = spec()
    driver.create(o)
    assert o.grafana_admin_password
    assert len(o.grafana_admin_password) == 20
    assert _values(calls)["grafana"]["adminPassword"] == o.grafana_admin_password


def test_two_installs_do_not_generate_the_same_password(driver, calls):
    a, b = spec(), spec()
    driver.create(a)
    driver.create(b)
    assert a.grafana_admin_password != b.grafana_admin_password


def test_an_existing_release_s_credentials_are_reused_not_rotated(driver, monkeypatch):
    """The chart writes the password into a Secret at install time.
    Regenerating one on `helm upgrade` would rotate a live Grafana's admin
    password out from under whoever is already using it.
    """
    recorded = []
    monkeypatch.setattr(driver, "_helm", lambda o: _FakeRunner(recorded))
    monkeypatch.setattr(kps, "require_cli", lambda *a, **k: None)
    monkeypatch.setattr(kps, "require_cluster", lambda *a, **k: None)
    monkeypatch.setattr(kps, "kubectl", lambda *a, **k: _secret_json("ops", "kept"))

    o = spec()
    driver.create(o)

    assert (o.grafana_admin_user, o.grafana_admin_password) == ("ops", "kept")
    assert _values(recorded)["grafana"]["adminPassword"] == "kept"


def test_a_caller_supplied_password_is_never_looked_up(driver, monkeypatch):
    recorded = []
    monkeypatch.setattr(driver, "_helm", lambda o: _FakeRunner(recorded))
    monkeypatch.setattr(kps, "require_cli", lambda *a, **k: None)
    monkeypatch.setattr(kps, "require_cluster", lambda *a, **k: None)
    monkeypatch.setattr(
        kps, "kubectl",
        lambda *a, **k: pytest.fail("must not read the Secret when told the password"),
    )

    driver.create(spec(grafana_admin_password="mine"))
    assert _values(recorded)["grafana"]["adminPassword"] == "mine"


def test_an_unreadable_secret_falls_back_to_generating(driver, monkeypatch):
    """Not JSON, a partial Secret, no Secret at all -- none of these are a
    reason to refuse to install."""
    recorded = []
    monkeypatch.setattr(driver, "_helm", lambda o: _FakeRunner(recorded))
    monkeypatch.setattr(kps, "require_cli", lambda *a, **k: None)
    monkeypatch.setattr(kps, "require_cluster", lambda *a, **k: None)
    monkeypatch.setattr(kps, "kubectl", lambda *a, **k: "not json at all")

    o = spec()
    driver.create(o)
    assert o.grafana_admin_password


def test_a_secret_missing_the_password_key_falls_back_to_generating(driver, monkeypatch):
    recorded = []
    monkeypatch.setattr(driver, "_helm", lambda o: _FakeRunner(recorded))
    monkeypatch.setattr(kps, "require_cli", lambda *a, **k: None)
    monkeypatch.setattr(kps, "require_cluster", lambda *a, **k: None)
    monkeypatch.setattr(
        kps, "kubectl",
        lambda *a, **k: json.dumps({"data": {"admin-user": base64.b64encode(b"ops").decode()}}),
    )

    o = spec()
    driver.create(o)
    assert o.grafana_admin_password


def test_the_secret_is_read_from_the_release_s_own_namespace(driver, calls):
    driver.create(spec(namespace="obs"))
    lookup = next(c for c in calls if c[0] == "kubectl")
    assert "-n" in lookup[1] and "obs" in lookup[1]
    assert "kube-prometheus-stack-grafana" in lookup[1]


# -- delete ---------------------------------------------------------------


def test_delete_uninstalls_the_release(driver, calls):
    driver.delete(spec())
    _, release, kwargs = next(c for c in calls if c[0] == "uninstall")
    assert release == "kube-prometheus-stack"
    assert kwargs["namespace"] == "monitoring"


def test_delete_tolerates_an_already_absent_release(driver, calls):
    # Teardown reruns, and a missing release is the desired end state.
    driver.delete(spec())
    assert next(c[2] for c in calls if c[0] == "uninstall")["missing_ok"] is True


def test_delete_leaves_the_persisted_data_alone(driver, calls):
    """helm uninstall never removes PVCs, and the driver does not either --
    metrics and dashboards survive a teardown unless removed by hand."""
    driver.delete(spec())
    assert not any(
        "pvc" in " ".join(str(x) for x in c).lower()
        or "persistentvolumeclaim" in " ".join(str(x) for x in c).lower()
        for c in calls
    )


def test_delete_validates_the_spec_first(driver, monkeypatch):
    monkeypatch.setattr(
        kps, "require_cli", lambda *a, **k: pytest.fail("should not reach the CLI")
    )
    with pytest.raises(ValueError):
        driver.delete(Observability(kubeconfig_path=KUBECONFIG, type="nope"))


# -- prerequisites --------------------------------------------------------


def test_prerequisites_require_helm_and_a_reachable_cluster(driver, monkeypatch):
    seen = []
    monkeypatch.setattr(kps, "require_cli", lambda name, **k: seen.append(name))
    monkeypatch.setattr(
        kps, "require_cluster", lambda path, **k: seen.append(("cluster", path))
    )

    assert driver.check_prerequisites(spec()) == []
    assert "helm" in seen
    assert ("cluster", KUBECONFIG) in seen


def test_a_missing_cluster_stops_create_before_helm_runs(driver, monkeypatch):
    monkeypatch.setattr(kps, "require_cli", lambda *a, **k: None)
    monkeypatch.setattr(
        kps, "require_cluster",
        lambda *a, **k: (_ for _ in ()).throw(
            ObservabilityPrerequisiteError("no cluster")
        ),
    )
    monkeypatch.setattr(
        driver, "_helm", lambda o: pytest.fail("helm must not run without a cluster")
    )

    with pytest.raises(ObservabilityPrerequisiteError):
        driver.create(spec())


def test_the_error_types_stay_catchable_by_capability():
    # A caller catches ObservabilityError, never a driver's own class.
    assert issubclass(ObservabilityPrerequisiteError, Exception)
    assert issubclass(ObservabilityError, Exception)
