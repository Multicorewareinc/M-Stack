# ServiceMonitor (Prometheus scraping)

Kubernetes `ServiceMonitor` support (the Prometheus Operator CRD that
kube-prometheus-stack's Prometheus reads) for the SDK's own services.
Every field this touches is empty by default, which means "no
ServiceMonitor installed" — the same ADR-006 shape as `netpolicy`'s
`allowed_client_labels`: a feature attaches by config, and an existing
deployment is untouched until a caller opts in.

`multistack/servicemonitor/manifest.py` holds the two things that are
actually shared:

- `service_monitor_manifest()` — a plain dict shaped like the k8s API
  (same style as `Inference.manifests()` and `netpolicy.manifest`).
- `apply_service_monitor()` — that dict plus `kube.apply()` in one call,
  for a workload with no manifest list of its own to append this to.

Everything else — which capability supports it, what field to set — lives
on the capability itself, described below.

## Where it applies

| Capability | Spec field | Selects | Port/path | Applied via |
|---|---|---|---|---|
| `Inference` (vLLM) | `service_monitor_labels` | `{"app": <name>}` | `inference.port` / `/metrics` | Python dict in `manifests()`, applied with the rest of the Deployment/Service |
| `Gateway`, `Policy` (rpm/tpm), `Tokenizer`, `Enricher`, `Billing`, `ControlPlane` (both) | `service_monitor_labels` | the chart's own selector labels | Service port `http` / `/metrics` | Helm values (`chart_values()` → `metrics.serviceMonitor`), rendered by each chart's `templates/servicemonitor.yaml` |

### The first-party charts

Every chart under `api/microservices/` (except `rag`) ships
`templates/servicemonitor.yaml` behind `metrics.serviceMonitor.enabled`.
The `prometheus.io/*` pod annotations those charts also set are **ignored
by kube-prometheus-stack** — they only help an annotation-driven
Prometheus — so without a ServiceMonitor none of these services is
scraped.

- **Install order.** The template renders only when the cluster serves
  `monitoring.coreos.com/v1`, so a service installed before
  `Observability` still installs (with no ServiceMonitor) instead of
  failing on "no matches for kind ServiceMonitor". Re-running its
  `create()` after the observability stack is up adds it.
- **NetworkPolicy.** With `networkPolicy.enabled`, the charts' ingress
  rule only admits same-namespace pods. When the ServiceMonitor is on they
  also admit `metrics.serviceMonitor.prometheusNamespace` (default
  `monitoring`) on the container port. The control-plane charts have no
  NetworkPolicy.
- **Stack wiring.** These specs read `service_monitor_labels` from the
  Stack (`FROM_STACK`), so publishing it once wires every service:
  `stack.provide(service_monitor_labels={"release": observability.options.release_name})`.
  `examples/full_stack.py` does this up front and runs the observability
  stage straight after storage and vault, so the CRD exists before the
  services install.

vLLM's OpenAI-compatible server exposes Prometheus metrics on `/metrics`
on the same port as the API **by default** — nothing needs to be passed
to the container to turn this on (only `--disable-log-stats` would turn
it *off*, and this driver never passes that). This package is only about
getting something to *scrape* that endpoint.

## Why this needs a label the caller supplies, not a bare `enabled: bool`

A `ServiceMonitor` sitting in a namespace does nothing on its own — it is
only consumed by a Prometheus whose own `serviceMonitorSelector` matches
its labels. kube-prometheus-stack's chart default
(`serviceMonitorSelectorNilUsesHelmValues: true` with an empty
`serviceMonitorSelector`) makes that mean **"has label `release:
<the Observability release's release_name>`"** — `kube-prometheus-stack`
unless `KubePrometheusStackOptions.release_name` was overridden (see
`examples/observability/install.py`).

Guessing that label here would either match nothing (silently — the
`ServiceMonitor` applies cleanly and nothing scrapes it) or, if guessed
generically, match every workload in the cluster carrying it. Same
principle `netpolicy`'s README states for `allowed_ingress`: a guessed
label is worse than none. So `service_monitor_labels` is supplied by the
caller, from the actual `Observability` spec they installed:

```python
from multistack import Inference, InferenceBackend, Observability

observability = Observability(kubeconfig_path=kc, storage_class="longhorn")
# observability.options.release_name defaults to "kube-prometheus-stack"

inference = Inference(
    kubeconfig_path=kc,
    name="vllm",
    service_monitor_labels={"release": observability.options.release_name},
)
InferenceBackend().create(inference)
```

Confirm the real selector on your own cluster before relying on it:

```
kubectl --kubeconfig <kc> -n monitoring get prometheus -o jsonpath='{.items[0].spec.serviceMonitorSelector}'
```

## Grafana

vLLM ships an official dashboard (`grafana.json`) alongside the same
upstream example this feature is based on
(https://docs.vllm.ai/en/v0.7.2/getting_started/examples/prometheus_grafana.html).
This package doesn't vendor that JSON or provision it automatically —
Grafana's dashboard sidecar (as kube-prometheus-stack configures it)
watches its **own** namespace, not `Inference`'s, so provisioning it here
would mean this driver reaching into `Observability`'s namespace, which
no other capability in this repo does. Import it by hand once
`ServiceMonitor` scraping is live: Grafana UI → Dashboards → Import →
paste the JSON, datasource = the Prometheus this stack installed
(`observability.prometheus_endpoint()`).

## Not covered, and why

Every other capability in this repo (`MinIOTenant`, `Portal`, …) either
isn't a Prometheus metrics source today, or exposes metrics through its
upstream chart's own monitor settings. `rag` is deliberately left out. Wiring one in later is the
same shape as `Inference`'s: append a `ServiceMonitor` object built with
`service_monitor_manifest()` when a new opt-in field is set — no change
needed here.
