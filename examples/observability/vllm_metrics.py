"""
Scrape vLLM's built-in Prometheus metrics with an already-installed
Observability stack (kube-prometheus-stack).

vLLM's OpenAI-compatible server exposes `/metrics` on the same port as the
API by default -- nothing to turn on there. This wires up the other half:
a `ServiceMonitor` (the Prometheus Operator CRD kube-prometheus-stack's
Prometheus reads) so that endpoint actually gets scraped. See
multistack/servicemonitor/README.md for the full design.

Assumes `examples/observability/install.py` and `examples/inference/serve.py`
(or similar) already ran against this cluster -- this only adds
`service_monitor_labels` on top of an existing `Inference` deployment; it's
a plain re-`create()`, no need to tear anything down first.

    pip install -e ".[helm]"                      # from the repo root
    python3 examples/observability/vllm_metrics.py
"""
import os

from multistack import Inference, InferenceBackend, Observability

KUBECONFIG = os.path.expanduser(
    os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig")
)

# Only used to read `options.release_name` below -- this doesn't install
# or touch the observability stack itself.
observability = Observability(kubeconfig_path=KUBECONFIG)

service = Inference(
    kubeconfig_path=KUBECONFIG,
    name="vllm-qwen",

    # kube-prometheus-stack's Prometheus only scrapes ServiceMonitors
    # carrying this label by default (its own
    # `serviceMonitorSelectorNilUsesHelmValues` convention) -- confirm
    # against your own cluster with:
    #   kubectl -n monitoring get prometheus -o \
    #     jsonpath='{.items[0].spec.serviceMonitorSelector}'
    service_monitor_labels={"release": observability.options.release_name},
)

backend = InferenceBackend()

# Idempotent re-apply: adds the ServiceMonitor alongside the existing
# Deployment/Service, touching neither.
endpoint = backend.create(service)

print(f"\nvLLM serving at {endpoint}")
print(f"Raw metrics:  {endpoint}/metrics")
print(f"Prometheus:   {observability.prometheus_endpoint()}")
print(
    "\nGrafana dashboard: import vLLM's official grafana.json "
    "(https://docs.vllm.ai/en/v0.7.2/getting_started/examples/prometheus_grafana.html) "
    f"against the Prometheus datasource above at {observability.grafana_endpoint()}"
)
