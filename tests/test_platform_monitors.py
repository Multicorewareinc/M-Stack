"""Prometheus monitors for the platform components (Valkey, NATS, CNPG, Longhorn, Istio).

Same shape as tests/test_servicemonitor.py: nothing shells out. The
servicemonitor module's `kubectl` (the CRD check) and `kube_apply` are
stubbed, so each test states whether the cluster "has" the CRDs.
"""
from __future__ import annotations

import pytest

from multistack.servicemonitor import (
    merge_values,
    monitors_wanted,
    pod_monitor_manifest,
)

KC = "/tmp/kc.yaml"
LABELS = {"release": "kube-prometheus-stack"}


@pytest.fixture
def cluster(monkeypatch):
    """A fake cluster: `.crds` decides what the CRD check sees, `.applied`
    records every manifest applied through the servicemonitor module."""

    class Cluster:
        def __init__(self) -> None:
            self.crds = True
            self.applied: list[dict] = []
            self.checks: list[tuple] = []

    state = Cluster()

    def fake_kubectl(kubeconfig_path, *args, check=True, error_cls=None, **_):
        state.checks.append(args)
        return f"customresourcedefinition/{args[2]}" if state.crds else ""

    def fake_apply(kubeconfig_path, manifest, *, error_cls):
        state.applied.append(manifest)

    monkeypatch.setattr("multistack.servicemonitor.manifest.kubectl", fake_kubectl)
    monkeypatch.setattr("multistack.servicemonitor.manifest.kube_apply", fake_apply)
    return state


# -- the shared helpers -------------------------------------------------------
def test_pod_monitor_manifest_shape():
    m = pod_monitor_manifest("pg-metrics", "db", {"cnpg.io/cluster": "pg"},
                             port="metrics", extra_labels=LABELS)
    assert m["kind"] == "PodMonitor"
    assert m["metadata"] == {"name": "pg-metrics", "namespace": "db", "labels": LABELS}
    assert m["spec"]["selector"] == {"matchLabels": {"cnpg.io/cluster": "pg"}}
    assert m["spec"]["namespaceSelector"] == {"matchNames": ["db"]}
    assert m["spec"]["podMetricsEndpoints"] == [
        {"port": "metrics", "path": "/metrics", "interval": "30s"}]


def test_no_labels_never_touches_the_cluster(cluster):
    assert monitors_wanted(KC, {}, log_prefix="t") is False
    assert cluster.checks == []


def test_a_missing_crd_is_a_warning_not_an_error(cluster, capsys):
    cluster.crds = False
    assert monitors_wanted(KC, LABELS, log_prefix="t") is False
    assert "not installed yet" in capsys.readouterr().out


def test_caller_values_win_over_monitor_values():
    merged = merge_values({"metrics": {"enabled": True, "serviceMonitor": {"enabled": True}}},
                          {"metrics": {"enabled": False}, "other": 1})
    assert merged == {"metrics": {"enabled": False, "serviceMonitor": {"enabled": True}},
                      "other": 1}


# -- Valkey -------------------------------------------------------------------
def _cache(**kwargs):
    from multistack.cache import Cache

    return Cache(kubeconfig_path=KC, name="valkey", namespace="cache", **kwargs)


def test_valkey_turns_on_the_exporter_and_its_service_monitor(cluster):
    from multistack.cache.drivers.valkey import ValkeyDriver

    values = ValkeyDriver()._values(_cache(service_monitor_labels=LABELS))
    assert values["metrics"]["enabled"] is True
    assert values["metrics"]["serviceMonitor"] == {"enabled": True, "additionalLabels": LABELS}


def test_valkey_values_are_untouched_without_labels_or_crd(cluster):
    from multistack.cache.drivers.valkey import ValkeyDriver

    assert "metrics" not in ValkeyDriver()._values(_cache())
    cluster.crds = False
    assert "metrics" not in ValkeyDriver()._values(_cache(service_monitor_labels=LABELS))


# -- NATS ---------------------------------------------------------------------
def test_nats_pod_monitor_rides_the_charts_prom_exporter():
    from multistack.queue.drivers.nats import JetStreamQueueDriver
    from multistack.queue.spec import Queue

    queue = Queue(kubeconfig_path=KC, storage_class="longhorn", service_monitor_labels=LABELS)
    driver = JetStreamQueueDriver()

    values = driver._deployment(queue, pod_monitor=True).helm_values()
    exporter = values["promExporter"]
    assert exporter["enabled"] is True
    assert exporter["podMonitor"] == {"enabled": True, "merge": {"metadata": {"labels": LABELS}}}

    plain = driver._deployment(queue).helm_values()
    assert not plain["promExporter"].get("podMonitor", {}).get("enabled")


# -- CloudNativePG --------------------------------------------------------------
def test_cnpg_pod_monitor_selects_the_clusters_instances(cluster):
    from multistack.database import Database, DatabaseConfig
    from multistack.database.drivers.cnpg import CNPGDriver

    db = Database(kubeconfig_path=KC, name="admin-pg", namespace="platform-db",
                  database=DatabaseConfig(name="admin", owner="admin", password="s3cret"),
                  service_monitor_labels=LABELS)
    CNPGDriver()._apply_monitor(db)

    [monitor] = cluster.applied
    assert monitor["kind"] == "PodMonitor"
    assert monitor["metadata"]["name"] == "admin-pg-metrics"
    assert monitor["metadata"]["namespace"] == "platform-db"
    assert monitor["metadata"]["labels"] == LABELS
    assert monitor["spec"]["selector"] == {"matchLabels": {"cnpg.io/cluster": "admin-pg"}}
    assert monitor["spec"]["podMetricsEndpoints"][0]["port"] == "metrics"


# -- Longhorn -----------------------------------------------------------------
def test_longhorn_monitor_scrapes_the_manager(cluster, monkeypatch):
    from multistack.storage import Storage
    from multistack.storage.drivers import longhorn
    from multistack.storage.drivers.longhorn import LonghornDriver

    monkeypatch.setattr(longhorn, "kubectl", lambda *a, **k: "")  # no Longhorn NetworkPolicy

    storage = Storage(type="longhorn", kubeconfig_path=KC, service_monitor_labels=LABELS)
    assert LonghornDriver().apply_monitors(storage) is True

    [monitor] = cluster.applied
    assert monitor["kind"] == "ServiceMonitor"
    assert monitor["metadata"]["namespace"] == storage.resolved_namespace
    assert monitor["spec"]["selector"] == {"matchLabels": {"app": "longhorn-manager"}}
    assert monitor["spec"]["endpoints"][0]["port"] == "manager"


def test_longhorn_opens_its_own_network_policy_to_prometheus(cluster, monkeypatch):
    from multistack.storage import Storage
    from multistack.storage.drivers import longhorn

    applied = []
    monkeypatch.setattr(longhorn, "kubectl", lambda *a, **k: "networkpolicy/longhorn-manager")
    monkeypatch.setattr(longhorn, "apply", lambda kc, m, **k: applied.append(m))
    storage = Storage(type="longhorn", kubeconfig_path=KC, service_monitor_labels=LABELS)
    longhorn.LonghornDriver().apply_monitors(storage)

    [policy] = applied
    assert policy["kind"] == "NetworkPolicy"
    assert policy["spec"]["podSelector"] == {"matchLabels": {"app": "longhorn-manager"}}
    [rule] = policy["spec"]["ingress"]
    assert rule["from"][0]["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "prometheus"}
    assert rule["ports"] == [{"protocol": "TCP", "port": 9500}]


def test_longhorn_never_isolates_a_manager_no_policy_selects(cluster, monkeypatch):
    """Adding the only policy that selects a pod would cut off its other traffic."""
    from multistack.storage import Storage
    from multistack.storage.drivers import longhorn

    applied = []
    monkeypatch.setattr(longhorn, "kubectl", lambda *a, **k: "")
    monkeypatch.setattr(longhorn, "apply", lambda kc, m, **k: applied.append(m))
    storage = Storage(type="longhorn", kubeconfig_path=KC, service_monitor_labels=LABELS)
    assert longhorn.LonghornDriver().apply_monitors(storage) is True
    assert applied == []


def test_longhorn_monitor_waits_for_the_crd(cluster, monkeypatch):
    from multistack.storage import Storage
    from multistack.storage.drivers import longhorn
    from multistack.storage.drivers.longhorn import LonghornDriver

    monkeypatch.setattr(longhorn, "kubectl", lambda *a, **k: "")  # no Longhorn NetworkPolicy

    cluster.crds = False
    storage = Storage(type="longhorn", kubeconfig_path=KC, service_monitor_labels=LABELS)
    assert LonghornDriver().apply_monitors(storage) is False
    assert cluster.applied == []


# -- Istio ingress --------------------------------------------------------------
def test_istio_monitors_cover_envoy_and_istiod(cluster):
    from multistack.ingress_gateway import IngressGateway
    from multistack.ingress_gateway.drivers.metallb_istio import MetalLBIstioDriver

    gateway = IngressGateway(kubeconfig_path=KC, address_pool=["192.168.1.240-192.168.1.250"],
                             service_monitor_labels=LABELS)
    assert MetalLBIstioDriver().apply_monitors(gateway) is True

    envoy, istiod = cluster.applied
    assert envoy["kind"] == "PodMonitor"
    assert envoy["metadata"]["namespace"] == gateway.options.ingress_namespace
    assert envoy["spec"]["selector"] == {"matchLabels": {"app": gateway.options.ingress_release_name}}
    assert envoy["spec"]["podMetricsEndpoints"][0] == {
        "port": "http-envoy-prom", "path": "/stats/prometheus", "interval": "30s"}
    assert istiod["kind"] == "ServiceMonitor"
    assert istiod["metadata"]["namespace"] == gateway.options.istio_namespace
    assert istiod["spec"]["endpoints"][0]["port"] == "http-monitoring"
    assert all(m["metadata"]["labels"] == LABELS for m in cluster.applied)


@pytest.mark.parametrize("cls_path", [
    "multistack.cache.Cache", "multistack.queue.spec.Queue", "multistack.database.Database",
    "multistack.storage.Storage", "multistack.ingress_gateway.IngressGateway",
])
def test_every_platform_spec_reads_the_labels_from_the_stack(cls_path):
    import importlib

    module, name = cls_path.rsplit(".", 1)
    cls = getattr(importlib.import_module(module), name)
    assert cls.FROM_STACK["service_monitor_labels"] == "service_monitor_labels"
    assert cls.model_fields["service_monitor_labels"].default_factory() == {}
