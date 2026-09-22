"""Tests for the opt-in ServiceMonitor feature (multistack/servicemonitor/).

Scope is deliberately narrow, same shape as tests/test_netpolicy.py: only
that the field defaults to inert, that opting in produces the right
selector/port/labels, and that the shared builder/applier work correctly
on their own. Nothing shells out: `kube.apply` is monkeypatched wherever a
real call would need a cluster.
"""
from __future__ import annotations

from multistack import Inference
from multistack.inference.drivers.vllm import VLLMDriver
from multistack.servicemonitor import apply_service_monitor, service_monitor_manifest

KUBECONFIG = "/tmp/kc.yaml"


# -- the shared builder/applier ---------------------------------------------
def test_manifest_is_shaped_like_the_k8s_api():
    manifest = service_monitor_manifest(
        "vllm-metrics", "inference", {"app": "vllm"},
        port="http", extra_labels={"release": "kube-prometheus-stack"},
    )
    assert manifest["apiVersion"] == "monitoring.coreos.com/v1"
    assert manifest["kind"] == "ServiceMonitor"
    assert manifest["metadata"]["name"] == "vllm-metrics"
    assert manifest["metadata"]["namespace"] == "inference"
    assert manifest["metadata"]["labels"] == {"release": "kube-prometheus-stack"}
    spec = manifest["spec"]
    assert spec["selector"] == {"matchLabels": {"app": "vllm"}}
    assert spec["namespaceSelector"] == {"matchNames": ["inference"]}
    assert spec["endpoints"] == [{"port": "http", "path": "/metrics", "interval": "15s"}]


def test_manifest_path_and_interval_are_overridable():
    manifest = service_monitor_manifest(
        "x", "ns", {"app": "target"},
        port="metrics", path="/custom", interval="30s",
    )
    assert manifest["spec"]["endpoints"] == [
        {"port": "metrics", "path": "/custom", "interval": "30s"}
    ]


def test_manifest_extra_labels_default_empty():
    manifest = service_monitor_manifest("x", "ns", {"app": "target"}, port="http")
    assert manifest["metadata"]["labels"] == {}


def test_apply_service_monitor_builds_and_applies_the_same_manifest(monkeypatch):
    captured = {}

    def fake_apply(kubeconfig_path, manifest, *, error_cls):
        captured["kubeconfig_path"] = kubeconfig_path
        captured["manifest"] = manifest
        captured["error_cls"] = error_cls

    monkeypatch.setattr("multistack.servicemonitor.manifest.kube_apply", fake_apply)

    class MyError(Exception):
        pass

    apply_service_monitor(
        KUBECONFIG, "x", "ns", {"app": "target"},
        port="http", extra_labels={"release": "kube-prometheus-stack"},
        error_cls=MyError,
    )

    assert captured["kubeconfig_path"] == KUBECONFIG
    assert captured["error_cls"] is MyError
    assert captured["manifest"] == service_monitor_manifest(
        "x", "ns", {"app": "target"},
        port="http", extra_labels={"release": "kube-prometheus-stack"},
    )


# -- Inference (vLLM) --------------------------------------------------------
def test_vllm_manifests_omit_service_monitor_by_default():
    driver = VLLMDriver()
    inf = Inference(kubeconfig_path=KUBECONFIG, name="vllm")
    kinds = [o["kind"] for o in driver.manifests(inf)]
    assert "ServiceMonitor" not in kinds


def test_vllm_service_monitor_matches_the_deployment_and_service():
    driver = VLLMDriver()
    inf = Inference(
        kubeconfig_path=KUBECONFIG, name="vllm",
        service_monitor_labels={"release": "kube-prometheus-stack"},
    )
    objs = driver.manifests(inf)
    monitors = [o for o in objs if o["kind"] == "ServiceMonitor"]
    assert len(monitors) == 1
    monitor = monitors[0]

    assert monitor["metadata"]["name"] == "vllm-metrics"
    assert monitor["metadata"]["namespace"] == inf.resolved_namespace
    assert monitor["metadata"]["labels"] == {"release": "kube-prometheus-stack"}
    # Same selector the Deployment/Service themselves use.
    assert monitor["spec"]["selector"] == {"matchLabels": {"app": "vllm"}}
    # The Service names its port "http" -- endpoints resolve by name.
    assert monitor["spec"]["endpoints"] == [
        {"port": "http", "path": "/metrics", "interval": "15s"}
    ]


def test_vllm_service_monitor_does_not_disturb_other_manifests():
    driver = VLLMDriver()
    inf = Inference(
        kubeconfig_path=KUBECONFIG, name="vllm",
        service_monitor_labels={"release": "kube-prometheus-stack"},
        allowed_client_labels=[{"app.kubernetes.io/name": "model-gateway"}],
    )
    kinds = [o["kind"] for o in driver.manifests(inf)]
    assert kinds == ["Namespace", "Deployment", "Service", "NetworkPolicy", "ServiceMonitor"]
