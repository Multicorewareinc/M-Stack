"""Tests for the opt-in NetworkPolicy feature (multistack/netpolicy/).

Scope is deliberately narrow: only the NetworkPolicy-specific behavior of
each capability -- that the field defaults to inert, that opting in
produces the right selector/port, and that the shared builder/applier
work correctly on their own. Everything else about these capabilities
(rate limits, S3 mirroring, PVC sizing, ...) is covered by their own test
files and is out of scope here.

Nothing shells out: `apply_network_policy`/`kube.apply` are monkeypatched
wherever a real call would need a cluster.
"""
from __future__ import annotations

import pytest

from multistack import Gateway, Inference, MinIOTenant, Policy
from multistack.backends.minio_client import MinIOBackend
from multistack.gateway.drivers.modelgateway import ModelGatewayDriver
from multistack.inference.drivers.vllm import VLLMDriver
from multistack.netpolicy import apply_network_policy, network_policy_manifest
from multistack.policy.drivers.rpm import RPMDriver
from multistack.policy.drivers.tpm import TPMDriver
from multistack.state import tracking

KUBECONFIG = "/tmp/kc.yaml"


@pytest.fixture
def cluster_and_storage_ready():
    """MinIOTenant.REQUIRES = ("cluster", "storage"), and @track_create
    refuses to run create() at all without a healthy row for each -- see
    tests/backends/test_minio_client.py's `backend` fixture, which seeds
    the same two rows for the same reason."""
    state = tracking.default_state_manager()
    for name, component in (("test-cluster", "cluster"), ("test-storage", "storage")):
        state.start(name, component_type=component)
        state.mark_healthy(name)


# -- the shared builder/applier ---------------------------------------------
def test_manifest_is_ingress_only_and_shaped_like_the_k8s_api():
    manifest = network_policy_manifest(
        "vllm-allow-clients", "inference", {"app": "vllm"},
        allowed_ingress=[{"app.kubernetes.io/name": "model-gateway"}],
        ports=[8000],
    )
    assert manifest["apiVersion"] == "networking.k8s.io/v1"
    assert manifest["kind"] == "NetworkPolicy"
    assert manifest["metadata"] == {"name": "vllm-allow-clients", "namespace": "inference"}
    spec = manifest["spec"]
    assert spec["podSelector"] == {"matchLabels": {"app": "vllm"}}
    # Ingress-only by design -- see multistack/netpolicy/README.md.
    assert spec["policyTypes"] == ["Ingress"]
    assert "egress" not in spec


def test_manifest_allows_every_selector_listed():
    manifest = network_policy_manifest(
        "x", "ns", {"app": "target"},
        allowed_ingress=[{"app": "a"}, {"app": "b"}],
        ports=[80],
    )
    froms = manifest["spec"]["ingress"][0]["from"]
    assert froms == [
        {"podSelector": {"matchLabels": {"app": "a"}}},
        {"podSelector": {"matchLabels": {"app": "b"}}},
    ]


def test_manifest_allows_every_port_listed():
    manifest = network_policy_manifest(
        "x", "ns", {"app": "target"},
        allowed_ingress=[{"app": "a"}], ports=[80, 443],
    )
    ports = manifest["spec"]["ingress"][0]["ports"]
    assert ports == [
        {"protocol": "TCP", "port": 80},
        {"protocol": "TCP", "port": 443},
    ]


def test_apply_network_policy_builds_and_applies_the_same_manifest(monkeypatch):
    captured = {}

    def fake_apply(kubeconfig_path, manifest, *, error_cls):
        captured["kubeconfig_path"] = kubeconfig_path
        captured["manifest"] = manifest
        captured["error_cls"] = error_cls

    monkeypatch.setattr("multistack.netpolicy.manifest.kube_apply", fake_apply)

    class MyError(Exception):
        pass

    apply_network_policy(
        KUBECONFIG, "x", "ns", {"app": "target"},
        allowed_ingress=[{"app": "a"}], ports=[80], error_cls=MyError,
    )

    assert captured["kubeconfig_path"] == KUBECONFIG
    assert captured["error_cls"] is MyError
    assert captured["manifest"] == network_policy_manifest(
        "x", "ns", {"app": "target"}, allowed_ingress=[{"app": "a"}], ports=[80],
    )


# -- Inference (vLLM) --------------------------------------------------------
def test_vllm_manifests_omit_network_policy_by_default():
    driver = VLLMDriver()
    inf = Inference(kubeconfig_path=KUBECONFIG, name="vllm")
    kinds = [o["kind"] for o in driver.manifests(inf)]
    assert "NetworkPolicy" not in kinds


def test_vllm_network_policy_matches_the_deployment_and_service():
    driver = VLLMDriver()
    inf = Inference(
        kubeconfig_path=KUBECONFIG, name="vllm",
        allowed_client_labels=[{"app.kubernetes.io/name": "model-gateway"}],
    )
    objs = driver.manifests(inf)
    policies = [o for o in objs if o["kind"] == "NetworkPolicy"]
    assert len(policies) == 1
    policy = policies[0]

    assert policy["metadata"]["name"] == "vllm-allow-clients"
    assert policy["metadata"]["namespace"] == inf.resolved_namespace
    # Same selector the Deployment/Service themselves use -- the policy has
    # to target the actual pods, not a guess.
    assert policy["spec"]["podSelector"] == {"matchLabels": {"app": "vllm"}}
    assert policy["spec"]["ingress"][0]["from"] == [
        {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "model-gateway"}}}
    ]
    assert policy["spec"]["ingress"][0]["ports"] == [{"protocol": "TCP", "port": inf.port}]


# -- Gateway (model-gateway) -------------------------------------------------
def _gateway(**kwargs) -> Gateway:
    defaults = dict(
        kubeconfig_path=KUBECONFIG, upstream_url="http://vllm:8000",
        api_key_secret="gateway-keys",
        org_cp_internal_url="http://organization-control-plane:8000",
    )
    return Gateway(**{**defaults, **kwargs})


def test_gateway_values_omit_network_policy_by_default():
    values = ModelGatewayDriver()._values(_gateway())
    assert "networkPolicy" not in values


def test_gateway_values_enable_network_policy_with_the_given_selectors():
    values = ModelGatewayDriver()._values(
        _gateway(network_policy_allowed_ingress=[{"istio": "ingressgateway"}])
    )
    assert values["networkPolicy"] == {
        "enabled": True,
        "allowedIngress": [{"istio": "ingressgateway"}],
    }


# -- Policy (rate-limiter rpm/tpm) -------------------------------------------
def _policy(type_: str, **kwargs) -> Policy:
    defaults = dict(
        kubeconfig_path=KUBECONFIG, cache_url="redis://redis:6379/0",
        allow_no_backbone=True,
    )
    return Policy(type=type_, **{**defaults, **kwargs})


@pytest.mark.parametrize("policy_type,driver_cls", [("rpm", RPMDriver), ("tpm", TPMDriver)])
def test_policy_values_omit_network_policy_by_default(policy_type, driver_cls):
    values = driver_cls()._values(_policy(policy_type))
    assert "networkPolicy" not in values


@pytest.mark.parametrize("policy_type,driver_cls", [("rpm", RPMDriver), ("tpm", TPMDriver)])
def test_policy_values_enable_network_policy_with_the_given_selectors(policy_type, driver_cls):
    values = driver_cls()._values(_policy(
        policy_type,
        network_policy_allowed_ingress=[{"app.kubernetes.io/name": "model-gateway"}],
    ))
    assert values["networkPolicy"] == {
        "enabled": True,
        "allowedIngress": [{"app.kubernetes.io/name": "model-gateway"}],
    }


# -- MinIOTenant --------------------------------------------------------------
def _tenant(**kwargs) -> MinIOTenant:
    defaults = dict(kubeconfig_path=KUBECONFIG, name="models")
    return MinIOTenant(**{**defaults, **kwargs})


def test_minio_apply_network_policy_targets_the_operators_own_tenant_label(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "multistack.backends.minio_client.apply_network_policy",
        lambda *a, **k: captured.update(args=a, kwargs=k),
    )

    tenant = _tenant(network_policy_allowed_ingress=[{"app": "vllm"}])
    MinIOBackend()._apply_network_policy(tenant)

    args, kwargs = captured["args"], captured["kwargs"]
    assert args[0] == tenant.kubeconfig_path
    assert args[1] == "models-allow-clients"
    assert args[2] == tenant.namespace
    assert args[3] == {"v1.min.io/tenant": "models"}
    assert kwargs["allowed_ingress"] == [{"app": "vllm"}]


def test_minio_network_policy_targets_the_containers_real_port_not_the_service_port():
    """Regression test for a bug a live cluster run actually caught: this
    used to compute 443/80 (endpoint()'s *external* scheme-implied port),
    but NetworkPolicy `ports` matches the pod's real listening port after
    Kubernetes DNATs the Service -- the minio/tenant chart always targets
    container port 9000 regardless of request_auto_cert. The old value
    left 9000 unmatched, which silently blocked every client, allowed or
    not, once the policy existed."""
    captured = {}

    def fake_apply(kubeconfig_path, name, namespace, pod_selector, *,
                    allowed_ingress, ports, error_cls):
        captured["ports"] = ports

    import multistack.backends.minio_client as minio_client
    orig = minio_client.apply_network_policy
    minio_client.apply_network_policy = fake_apply
    try:
        MinIOBackend()._apply_network_policy(
            _tenant(network_policy_allowed_ingress=[{"app": "vllm"}])
        )
    finally:
        minio_client.apply_network_policy = orig

    assert captured["ports"] == [9000]


@pytest.mark.parametrize("request_auto_cert", [True, False])
def test_minio_network_policy_port_is_9000_regardless_of_tls(request_auto_cert, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "multistack.backends.minio_client.apply_network_policy",
        lambda *a, ports, **k: captured.update(ports=ports),
    )
    MinIOBackend()._apply_network_policy(_tenant(
        request_auto_cert=request_auto_cert,
        network_policy_allowed_ingress=[{"app": "vllm"}],
    ))
    assert captured["ports"] == [9000]


def test_minio_create_skips_network_policy_when_allow_list_is_empty(
    monkeypatch, cluster_and_storage_ready
):
    calls = []
    monkeypatch.setattr(
        MinIOBackend, "_apply_network_policy",
        lambda self, tenant: calls.append(tenant),
    )
    monkeypatch.setattr(MinIOBackend, "check_prerequisites", lambda self, t: [])
    monkeypatch.setattr(MinIOBackend, "_install_operator", lambda self, t: None)
    monkeypatch.setattr(MinIOBackend, "_deploy_tenant", lambda self, t: None)
    monkeypatch.setattr(MinIOBackend, "_find_release", lambda self, t: None)

    MinIOBackend().create(_tenant(root_user="u", root_password="p"), wait_for_ready=False)

    assert calls == []


def test_minio_create_applies_network_policy_when_allow_list_is_set(
    monkeypatch, cluster_and_storage_ready
):
    calls = []
    monkeypatch.setattr(
        MinIOBackend, "_apply_network_policy",
        lambda self, tenant: calls.append(tenant),
    )
    monkeypatch.setattr(MinIOBackend, "check_prerequisites", lambda self, t: [])
    monkeypatch.setattr(MinIOBackend, "_install_operator", lambda self, t: None)
    monkeypatch.setattr(MinIOBackend, "_deploy_tenant", lambda self, t: None)
    monkeypatch.setattr(MinIOBackend, "_find_release", lambda self, t: None)

    tenant = _tenant(
        root_user="u", root_password="p",
        network_policy_allowed_ingress=[{"app": "vllm"}],
    )
    MinIOBackend().create(tenant, wait_for_ready=False)

    assert calls == [tenant]


def test_minio_delete_always_attempts_network_policy_cleanup(monkeypatch):
    """Not part of the Helm release, so `helm uninstall` won't remove it --
    delete() has to ask for it explicitly, whether or not one was ever
    applied (--ignore-not-found makes the no-op case safe)."""
    recorded = []
    backend = MinIOBackend()
    monkeypatch.setattr(backend, "_require_cli", lambda name: None)
    monkeypatch.setattr(backend, "_find_release", lambda t: None)
    monkeypatch.setattr(
        backend, "_kubectl",
        lambda t, *args, **kwargs: recorded.append(args) or "",
    )

    backend.delete(_tenant())

    assert (
        "delete", "networkpolicy", "models-allow-clients",
        "-n", "minio", "--ignore-not-found",
    ) in recorded
