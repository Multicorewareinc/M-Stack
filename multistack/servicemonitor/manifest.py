"""One place to build a `ServiceMonitor` object.

Same shape as `multistack.netpolicy.manifest`: a plain dict matching the
Kubernetes API, built by a capability's own `manifests()` and applied with
the rest of its objects via `multistack.kube`.

`ServiceMonitor` (`monitoring.coreos.com/v1`) is a Prometheus Operator
CRD, not a core Kubernetes kind — it only exists once an Operator is
installed to watch for it. `Observability` (`kube_prometheus_stack`)
installs one; nothing here talks to that capability directly, so a
`ServiceMonitor` can be rendered even against a cluster that doesn't have
the CRD yet (it just sits unconsumed until the Operator shows up), and
`Observability` never has to know a given workload's Service labels.

The Prometheus instance kube-prometheus-stack installs only scrapes
`ServiceMonitor`s that match its own `serviceMonitorSelector` — which, by
that chart's default, means carrying a `release: <observability
release_name>` label (see `examples/observability/install.py` and this
package's README). That label is never guessed here: `extra_labels` is
supplied by
the caller, same principle as `netpolicy`'s `allowed_ingress` — a wrong
guess either matches nothing or matches too much.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..kube import KubeCommandError, kubectl
from ..kube import apply as kube_apply

SERVICE_MONITOR_CRD = "servicemonitors.monitoring.coreos.com"
POD_MONITOR_CRD = "podmonitors.monitoring.coreos.com"


def service_monitor_manifest(
    name: str,
    namespace: str,
    match_labels: Dict[str, str],
    *,
    port: str,
    path: str = "/metrics",
    interval: str = "15s",
    extra_labels: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """A `ServiceMonitor` scraping `path` on `port` (a Service port *name*,
    not a number — Prometheus Operator resolves it per-endpoint) of every
    Service matching `match_labels` in `namespace`.

    `extra_labels` land on the `ServiceMonitor`'s own metadata, where a
    Prometheus's `serviceMonitorSelector` looks for them — not on
    `match_labels`, which instead is the Service selector this object
    uses to find what to scrape.
    """
    return {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "ServiceMonitor",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": dict(extra_labels or {}),
        },
        "spec": {
            "selector": {"matchLabels": match_labels},
            "namespaceSelector": {"matchNames": [namespace]},
            "endpoints": [{"port": port, "path": path, "interval": interval}],
        },
    }


def apply_service_monitor(
    kubeconfig_path: str,
    name: str,
    namespace: str,
    match_labels: Dict[str, str],
    *,
    port: str,
    path: str = "/metrics",
    interval: str = "15s",
    extra_labels: Optional[Dict[str, str]] = None,
    error_cls: type = KubeCommandError,
) -> None:
    """Builds and applies a `ServiceMonitor` in one call, for a workload
    with no manifest list of its own to append this to."""
    manifest = service_monitor_manifest(
        name, namespace, match_labels,
        port=port, path=path, interval=interval, extra_labels=extra_labels,
    )
    kube_apply(kubeconfig_path, manifest, error_cls=error_cls)


def chart_values(labels: dict[str, str] | None) -> dict[str, Any]:
    """Helm values turning on a first-party chart's own ServiceMonitor.

    For the capabilities installed from a chart under `api/microservices/`
    (Gateway, Policy, Billing, ControlPlane, Enricher, Tokenizer): each
    chart ships `templates/servicemonitor.yaml` behind
    `metrics.serviceMonitor.enabled`, so the driver only has to pass the
    selector labels through rather than apply a second object itself.

    Empty `labels` returns `{}` -- the chart default (no ServiceMonitor) is
    left alone, for the same reason `service_monitor_manifest` never
    guesses a label.
    """
    if not labels:
        return {}
    return {"metrics": {"serviceMonitor": {"enabled": True, "labels": dict(labels)}}}


def pod_monitor_manifest(
    name: str,
    namespace: str,
    match_labels: dict[str, str],
    *,
    port: str,
    path: str = "/metrics",
    interval: str = "30s",
    extra_labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    """A `PodMonitor` scraping `path` on the container port *named*
    `port` of every pod matching `match_labels` in `namespace`.

    For workloads whose metrics port is on the pod but not on any Service
    (CloudNativePG instances, Envoy sidecars). Same label contract as
    `service_monitor_manifest`: `extra_labels` is what the Prometheus's
    `podMonitorSelector` matches on.
    """
    return {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PodMonitor",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": dict(extra_labels or {}),
        },
        "spec": {
            "selector": {"matchLabels": match_labels},
            "namespaceSelector": {"matchNames": [namespace]},
            "podMetricsEndpoints": [{"port": port, "path": path, "interval": interval}],
        },
    }


def crd_installed(
    kubeconfig_path: str,
    crd: str = SERVICE_MONITOR_CRD,
    *,
    error_cls: type = KubeCommandError,
) -> bool:
    """Whether the cluster serves a Prometheus Operator CRD yet.

    Applying a ServiceMonitor/PodMonitor -- or installing an upstream chart
    with its monitor turned on -- before `Observability` has installed the
    CRDs fails the whole operation with "no matches for kind". Drivers
    check this first and skip (with a warning) instead, so a capability
    installed before the observability stack still installs.
    """
    out = kubectl(
        kubeconfig_path, "get", "crd", crd, "-o", "name",
        check=False, error_cls=error_cls,
    )
    return bool((out or "").strip())


def monitors_wanted(
    kubeconfig_path: str,
    labels: dict[str, str] | None,
    *,
    crd: str = SERVICE_MONITOR_CRD,
    log_prefix: str,
    error_cls: type = KubeCommandError,
) -> bool:
    """Whether a driver should turn its monitor on: labels were asked for
    and the cluster has the CRD. A missing CRD is a printed warning, not an
    error -- the capability itself installs fine, and a re-run after the
    observability stack adds the monitor."""
    if not labels:
        return False
    if crd_installed(kubeconfig_path, crd, error_cls=error_cls):
        return True
    print(
        f"[{log_prefix}] warning: {crd} is not installed yet, so no monitor was "
        "created. Re-run this after the observability stack is up."
    )
    return False


def apply_monitors(
    kubeconfig_path: str,
    manifests: list[dict[str, Any]],
    labels: dict[str, str] | None,
    *,
    crd: str = SERVICE_MONITOR_CRD,
    log_prefix: str,
    error_cls: type = KubeCommandError,
) -> bool:
    """Applies ServiceMonitor/PodMonitor `manifests` when `monitors_wanted`.
    Both CRDs ship together in kube-prometheus-stack; `crd` names the one
    these manifests need. Returns whether anything was applied."""
    if not monitors_wanted(
        kubeconfig_path, labels, crd=crd, log_prefix=log_prefix, error_cls=error_cls,
    ):
        return False
    for manifest in manifests:
        kube_apply(kubeconfig_path, manifest, error_cls=error_cls)
    print(f"[{log_prefix}] applied {len(manifests)} Prometheus monitor(s)")
    return True


def merge_values(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """`override` deep-merged over `base`, as a new dict. For layering a
    monitor's chart values *under* a caller's own values, so anything the
    caller set explicitly still wins."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_values(merged[key], value)
        else:
            merged[key] = value
    return merged
