---
name: observability
description: Use this skill when the user's request mentions monitoring, metrics, dashboards, alerting, Prometheus, Grafana, Alertmanager, or observability. Covers the real MultiStack SDK Observability/ObservabilityBackend classes.
---

# MultiStack SDK — Observability / ObservabilityBackend

Real class signature (from `multistack`, re-exported at the top level as
`from multistack import Observability, ObservabilityBackend`).
`kube_prometheus_stack` is the only implementation: Prometheus,
Alertmanager and Grafana from one Helm chart, which also ships
node-exporter and kube-state-metrics.

```python
class KubePrometheusStackOptions(BaseModel):
    release_name: str = "kube-prometheus-stack"
    chart: str = "kube-prometheus-stack"
    grafana_service_type: str = "ClusterIP"

class Observability(CapabilitySpec):
    type: str = "kube_prometheus_stack"
    kubeconfig_path: str                # required -- the EXISTING cluster
    namespace: Optional[str] = None
    chart_version: Optional[str] = None

    storage_class: Optional[str] = None # None = cluster's default StorageClass
    metrics_retention: str = "15d"      # must look like "15d", "6h", "4w"
    prometheus_volume_size: str = "20Gi"    # real k8s quantity, needs a unit
    grafana_volume_size: str = "5Gi"
    alertmanager_volume_size: str = "2Gi"

    grafana_admin_user: str = "admin"
    grafana_admin_password: Optional[str] = None  # see rules below
    extra_values: Dict[str, Any] = {}
    options: Optional[KubePrometheusStackOptions] = None

    def grafana_endpoint(self) -> str: ...       # real http://... URL, no credential
    def prometheus_endpoint(self) -> str: ...
    def alertmanager_endpoint(self) -> str: ...
```

Rules when generating code:
- `kubeconfig_path` is required and has no ambient fallback — the SDK
  deliberately never reads `$KUBECONFIG`/`~/.kube/config`.
- **Persistent by default, same as MinIO**: `REQUIRES = ("cluster",
  "storage")` — a StorageClass must already exist on the cluster.
  Without one, every Prometheus sample and every Grafana dashboard is
  lost on a routine pod reschedule. Don't propose installing this
  before storage exists.
- `metrics_retention` must be a real Prometheus duration (a number plus
  a unit, e.g. `"15d"`, `"6h"`) — a bare number is silently invalid to
  the chart, not a number of days.
- `prometheus_volume_size`/`grafana_volume_size`/
  `alertmanager_volume_size` must be real Kubernetes quantities with a
  unit (e.g. `"20Gi"`) — a bare number is bytes.
- **Never invent `grafana_admin_password`.** Leave it `None` — the
  driver generates one on first install and reads it back from the live
  Secret on every later call, so re-running `create()` never rotates it
  out from under whoever already has it.
- `grafana_endpoint()`/`prometheus_endpoint()`/`alertmanager_endpoint()`
  are real, computed in-cluster URLs, no credential in any of them —
  quote these rather than guessing a service name.
