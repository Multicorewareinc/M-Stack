"""The control plane capability: spec, wiring, and the FastAPI driver.

The theme of these tests is that nothing here may carry a credential. The
charts read DATABASE_URL, VALKEY_URL and JWT_SECRET from a Kubernetes
Secret; this spec carries only that Secret's name, and the driver tells
the chart not to render one of its own.
"""
import base64
import json

import pytest

from multistack.controlplane import (
    ControlPlane,
    ControlPlanePrerequisiteError,
    REQUIRED_SECRET_KEYS,
)
from multistack.controlplane.drivers import fastapi_service as cp
from multistack.controlplane.spec import AdminControlPlaneOptions
from multistack.stack import CAPABILITY_OUTPUT, Stack

KUBECONFIG = "/tmp/kc.yaml"


def spec(**kwargs) -> ControlPlane:
    kwargs.setdefault("existing_secret", "cp-secrets")
    return ControlPlane(kubeconfig_path=KUBECONFIG, **kwargs)


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
    return cp.FastAPIControlPlaneDriver()


@pytest.fixture
def calls(driver, monkeypatch):
    """Stubs the outward calls; the Secret exists and is complete."""
    recorded = []
    monkeypatch.setattr(driver, "_helm", lambda c: _FakeRunner(recorded))
    monkeypatch.setattr(cp, "require_cli", lambda *a, **k: None)
    monkeypatch.setattr(cp, "require_cluster", lambda *a, **k: None)
    monkeypatch.setattr(
        cp, "kubectl",
        lambda *a, **k: _secret(REQUIRED_SECRET_KEYS["admin"]),
    )
    return recorded


def _install(calls):
    return next(c[2] for c in calls if c[0] == "install")


# -- spec -----------------------------------------------------------------


def test_the_secret_name_is_required():
    with pytest.raises(ValueError, match="existing_secret is required"):
        ControlPlane(kubeconfig_path=KUBECONFIG, existing_secret="")


def test_no_credential_field_exists_on_the_spec():
    """The guard that matters: a spec is a file people commit, so there
    must be nowhere in it for a password to go."""
    forbidden = {
        "database_url", "valkey_url", "jwt_secret", "admin_api_key",
        "service_api_key", "password",
    }
    assert not (forbidden & set(ControlPlane.model_fields))


def test_both_types_are_supported():
    assert set(ControlPlane.SUPPORTED_TYPES) == {"admin", "organization"}


def test_each_type_gets_its_own_chart():
    assert spec(type="admin").options.chart.endswith("admin-control-plane/chart")
    assert spec(type="organization").options.chart.endswith(
        "organization-control-plane/chart"
    )


def test_each_type_points_at_the_other_as_its_peer():
    # Neither chart works alone: each calls the other over the cluster
    # network, and the default has to be right or a fresh install needs
    # hand-wiring.
    assert "organization-control-plane" in spec(type="admin").resolved_peer_url
    assert "admin-control-plane" in spec(type="organization").resolved_peer_url


def test_an_explicit_peer_url_wins():
    assert spec(peer_url="http://other:9000").resolved_peer_url == "http://other:9000"


def test_the_endpoint_is_credential_free():
    assert spec().endpoint == (
        "http://admin-control-plane.control-plane.svc.cluster.local:8000"
    )


def test_admin_needs_more_secret_keys_than_organization():
    # Only the admin plane holds a platform-wide API key.
    assert "ADMIN_API_KEY" in REQUIRED_SECRET_KEYS["admin"]
    assert "ADMIN_API_KEY" not in REQUIRED_SECRET_KEYS["organization"]


# -- wiring ---------------------------------------------------------------


def test_it_requires_a_cluster_a_database_and_a_cache():
    """The cache is not optional: the login path reads lockout counters
    before verifying a password, so with no cache every sign-in is a
    500."""
    assert ControlPlane.REQUIRES == ("cluster", "database", "cache")


def test_it_does_not_wire_database_url_from_the_stack():
    """The Stack's database_url is deliberately credential-free, so it
    cannot serve as the chart's DATABASE_URL. The Secret does."""
    assert "database_url" not in ControlPlane.FROM_STACK.values()
    assert "cache_url" not in ControlPlane.FROM_STACK.values()


def test_it_publishes_a_controlplane_endpoint():
    assert ControlPlane.PROVIDES == {"controlplane_endpoint": "endpoint"}
    assert CAPABILITY_OUTPUT["controlplane"] == "controlplane_endpoint"


def test_building_it_without_a_database_says_so():
    stack = Stack(KUBECONFIG)
    with pytest.raises(Exception, match="requires the 'database' capability"):
        stack.build(ControlPlane)


def test_recording_it_publishes_its_endpoint():
    stack = Stack(KUBECONFIG)
    stack.record(spec())
    assert stack._values["controlplane_endpoint"] == spec().endpoint


# -- driver ---------------------------------------------------------------


def test_create_installs_the_right_release(driver, calls):
    assert driver.create(spec()) == spec().endpoint
    _, release, kwargs = next(c for c in calls if c[0] == "install")
    assert release == "admin-control-plane"
    assert kwargs["namespace"] == "control-plane"


def test_the_chart_is_told_not_to_render_its_own_secret(driver, calls):
    """secret.create=false means no rendered manifest can contain a
    credential, whatever else is passed."""
    driver.create(spec())
    secret = _install(calls)["values"]["secret"]
    assert secret == {"existingSecret": "cp-secrets", "create": False}


def test_no_value_sent_to_helm_looks_like_a_credential(driver, calls):
    driver.create(spec())
    rendered = json.dumps(_install(calls)["values"]).lower()
    for token in ("password", "jwt_secret", "postgresql://", "redis://"):
        assert token not in rendered


def test_admin_sends_its_own_config_keys(driver, calls):
    driver.create(spec(type="admin"))
    config = _install(calls)["values"]["config"]
    assert "orgCpInternalUrl" in config
    assert config["seedPlans"] is True


def test_organization_sends_its_own_config_keys(driver, monkeypatch):
    recorded = []
    monkeypatch.setattr(driver, "_helm", lambda c: _FakeRunner(recorded))
    monkeypatch.setattr(cp, "require_cli", lambda *a, **k: None)
    monkeypatch.setattr(cp, "require_cluster", lambda *a, **k: None)
    monkeypatch.setattr(
        cp, "kubectl",
        lambda *a, **k: _secret(REQUIRED_SECRET_KEYS["organization"]),
    )

    driver.create(spec(type="organization"))
    config = _install(recorded)["values"]["config"]
    assert "adminCpInternalUrl" in config
    # Seeding is an admin-only concern; sending it here would be rejected
    # by the chart's strict value validation.
    assert "seedPlans" not in config


def test_a_missing_secret_is_refused_with_the_keys_it_needs(driver, monkeypatch):
    monkeypatch.setattr(cp, "require_cli", lambda *a, **k: None)
    monkeypatch.setattr(cp, "require_cluster", lambda *a, **k: None)
    monkeypatch.setattr(cp, "kubectl", lambda *a, **k: "")
    monkeypatch.setattr(driver, "_helm", lambda c: pytest.fail("must not install"))

    with pytest.raises(ControlPlanePrerequisiteError, match="DATABASE_URL"):
        driver.create(spec())


def test_a_secret_missing_one_key_is_refused_by_name(driver, monkeypatch):
    """The chart installs and the pod goes Ready without these. The
    failure arrives later, as a 500 on the first sign-in."""
    partial = [k for k in REQUIRED_SECRET_KEYS["admin"] if k != "VALKEY_URL"]
    monkeypatch.setattr(cp, "require_cli", lambda *a, **k: None)
    monkeypatch.setattr(cp, "require_cluster", lambda *a, **k: None)
    monkeypatch.setattr(cp, "kubectl", lambda *a, **k: _secret(partial))
    monkeypatch.setattr(driver, "_helm", lambda c: pytest.fail("must not install"))

    with pytest.raises(ControlPlanePrerequisiteError, match="VALKEY_URL"):
        driver.create(spec())


def test_seeding_with_two_replicas_warns(driver, calls):
    """seed_plans is idempotent per row but not concurrency-safe: two
    pods racing on a first install both insert the same plan and one dies
    on the unique index."""
    warnings = driver.check_prerequisites(spec(replicas=2))
    assert any("concurrency-safe" in w for w in warnings)


def test_seeding_off_produces_no_warning(driver, calls):
    o = AdminControlPlaneOptions(seed_plans=False)
    assert driver.check_prerequisites(spec(replicas=2, options=o)) == []


def test_delete_removes_the_release_but_not_the_database(driver, calls):
    driver.delete(spec())
    kinds = [c[0] for c in calls]
    assert "uninstall" in kinds
    # Nothing here should touch the database capability.
    assert not any("drop" in str(c).lower() for c in calls)


# -- scheduling -----------------------------------------------------------


def test_a_node_selector_reaches_the_chart(driver, calls):
    """The control plane images are sideloaded onto one node, so a pod
    scheduled elsewhere never pulls."""
    driver.create(spec(node_selector={"kubernetes.io/hostname": "rke2-wrk-2"}))
    assert _install(calls)["values"]["nodeSelector"] == {
        "kubernetes.io/hostname": "rke2-wrk-2"
    }


def test_no_node_selector_means_the_key_is_absent(driver, calls):
    driver.create(spec())
    assert "nodeSelector" not in _install(calls)["values"]


def test_tolerations_reach_the_chart(driver, calls):
    tol = [{"key": "dedicated", "operator": "Exists"}]
    driver.create(spec(tolerations=tol))
    assert _install(calls)["values"]["tolerations"] == tol
