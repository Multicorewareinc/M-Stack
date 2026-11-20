"""The dashboards shipped with the SDK, for any observability implementation.

`dashboards/*.json` are Grafana dashboards over the metrics this platform
exposes (the first-party services' `http_metrics.py` and domain counters,
vLLM, and the platform components' exporters). They query a `${datasource}`
variable rather than a fixed datasource uid, so they work against whichever
Prometheus the implementation installs.

Implementation-neutral on purpose: what a driver does with them (a
ConfigMap for Grafana's dashboard sidecar, an API upload, ...) is its own
business.
"""
from __future__ import annotations

from importlib import resources

# Label/annotation kube-prometheus-stack's Grafana sidecar reads (its chart
# defaults: `grafana.sidecar.dashboards.label/labelValue`), and the folder
# annotation the driver turns on.
DASHBOARD_LABEL = "grafana_dashboard"
DASHBOARD_FOLDER_ANNOTATION = "grafana_folder"
DASHBOARD_FOLDER = "Multistack"
# Marks the ConfigMaps this SDK owns, so delete() removes exactly those.
MANAGED_BY_LABEL = {"app.kubernetes.io/managed-by": "multistack"}


def dashboards() -> dict[str, str]:
    """`{name: dashboard JSON text}` for every shipped dashboard."""
    root = resources.files(__package__) / "dashboards"
    return {
        entry.name.removesuffix(".json"): entry.read_text(encoding="utf-8")
        for entry in sorted(root.iterdir(), key=lambda e: e.name)
        if entry.name.endswith(".json")
    }
