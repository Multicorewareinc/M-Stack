"""
Install cluster observability (Prometheus + Alertmanager + Grafana) on an
existing RKE2 cluster.

A separate, user-triggered step — like `examples/minio/tenant.py` and
`examples/ingress_gateway/install.py`. Nothing deploys this on its own.

    pip install -e ".[helm]"                  # from the repo root
    python3 examples/observability/install.py

Needs `helm` on PATH, and a cluster whose kubeconfig is below already up
and reachable with a working StorageClass (a fresh RKE2 cluster has none —
see examples/storage/install.py).
"""
import os

from multistack.observability import Observability, ObservabilityBackend

# Not /tmp: this is the same kubeconfig examples/rke2/cluster.py wrote, and
# losing it leaves you with a cluster you cannot reach.
KUBECONFIG = os.path.expanduser(
    os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig")
)
# Named explicitly. Left unset the cluster default is used, which is fine
# until someone changes which class is default.
STORAGE_CLASS = os.environ.get("MULTISTACK_STORAGE_CLASS", "longhorn")

observability = Observability(
    # Required — no ambient $KUBECONFIG fallback, so this can only ever
    # act on the cluster you name.
    kubeconfig_path=KUBECONFIG,
    storage_class=STORAGE_CLASS,
    namespace="monitoring",  # optional; defaults to `monitoring`

    # Prometheus's own duration format, not a bare number.
    metrics_retention="15d",

    # Leave grafana_admin_password unset and it's generated on first
    # install, then read back (never rotated) on every later run. It
    # comes back on the spec below.

    # Chart/release/namespace overrides live on
    # `options=KubePrometheusStackOptions(...)`; the default namespace
    # ("monitoring") works for a cluster that isn't already using it for
    # something else.
)

backend = ObservabilityBackend()

# Installs kube-prometheus-stack with --wait, and returns Grafana's
# in-cluster endpoint. Idempotent (helm upgrade --install).
endpoint = backend.create(observability)

print(f"\nObservability ready — Grafana at {endpoint}")
print(f"  user:     {observability.grafana_admin_user}")
print(f"  password: {observability.grafana_admin_password}")
print(f"\nPrometheus:   {observability.prometheus_endpoint()}")
print(f"Alertmanager: {observability.alertmanager_endpoint()}")
print(
    f"\nReach Grafana from outside the cluster with:\n"
    f"  KUBECONFIG={KUBECONFIG} kubectl -n {observability.resolved_namespace} "
    f"port-forward svc/{observability.options.release_name}-grafana 3000:80"
)
