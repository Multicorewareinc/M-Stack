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

from ..kube import KubeCommandError
from ..kube import apply as kube_apply


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
