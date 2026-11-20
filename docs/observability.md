# Observability: Prometheus and Grafana

How metrics, dashboards and alerts work across the stack, and how to turn
them on, check them and extend them. The `Observability` capability
(`kube_prometheus_stack`) installs Prometheus, Alertmanager and Grafana;
everything below is what feeds it.

## What gets scraped

kube-prometheus-stack's Prometheus only reads `ServiceMonitor` and
`PodMonitor` objects whose labels match its selector, `release: <its
release name>`. It ignores the `prometheus.io/*` pod annotations the charts
also set. So every component carries a `service_monitor_labels` field. Left
empty (the default), it creates no monitor. Set it to the selector's labels
and the component creates its own monitor:

| Component | Monitor | Created by |
|---|---|---|
| model-gateway, rate-limiter-rpm/tpm, tokenizer, enricher, billing, both control planes | ServiceMonitor from each chart's `templates/servicemonitor.yaml` | `helm install` (chart value `metrics.serviceMonitor`) |
| vLLM / KServe (`Inference`) | ServiceMonitor | `InferenceBackend.create()` |
| Valkey (`Cache`) | ServiceMonitor + redis-exporter sidecar | chart values |
| NATS (`Queue`) | PodMonitor on the prom-exporter sidecar | chart values |
| PostgreSQL (`Database`, CNPG) | PodMonitor per cluster | `create_cluster()` |
| Longhorn (`Storage`) | ServiceMonitor on longhorn-manager | `StorageBackend.apply_monitors()` |
| Istio ingress (`IngressGateway`) | PodMonitor on Envoy + ServiceMonitor on istiod | `IngressGatewayBackend.apply_monitors()` |

A monitor is created only once the cluster serves the Prometheus Operator
CRDs. Before that, charts render no monitor and drivers print a warning, so
the install still succeeds. Storage and the ingress always install *before*
observability (its PVCs need the StorageClass), so they get their monitors
from `apply_monitors()` afterwards. Design notes are in
[`multistack/servicemonitor/README.md`](../multistack/servicemonitor/README.md).

### With `examples/full_stack.py`

Monitoring is on by default:

- The script publishes `service_monitor_labels={"release": "kube-prometheus-stack"}`
  to the Stack before any stage runs, and every spec reads it from there.
  `MULTISTACK_SERVICE_MONITORS=0` turns it off.
- `observability` runs right after `storage` and `vault`, so later stages
  find the CRDs.
- `platform_metrics` runs near the end and applies the Longhorn and Istio
  monitors.
- Re-running a single stage keeps its monitor: the labels are still
  published, so `helm upgrade` doesn't remove the ServiceMonitor.

### With the SDK directly

```python
observability = Observability(kubeconfig_path=kc, storage_class="longhorn")
ObservabilityBackend().create(observability)

labels = {"release": observability.options.release_name}
stack.provide(service_monitor_labels=labels)          # every later stack.build() gets it
gateway = Gateway(..., service_monitor_labels=labels)  # or set it per spec
```

## Metrics the services expose

Every first-party service ships the same `src/http_metrics.py` middleware.
It is pure ASGI, so the gateway's SSE streaming is untouched.
`tests/test_http_metrics.py` keeps the copies identical.

| Metric | Labels |
|---|---|
| `http_requests_total` | `method`, `route` (template, e.g. `/v1/orgs/{org_id}`; unmatched → `<unmatched>`), `status` |
| `http_request_duration_seconds` | `method`, `route` |
| `http_requests_in_flight` | — |

`/metrics` and `/health` are not counted. Prometheus adds `namespace`,
`service`, `pod` and `job`, so one query covers every service.

Domain metrics:

| Service | Metrics |
|---|---|
| model-gateway | `gw_upstream_requests_total{model,upstream_type,outcome}`, `gw_upstream_response_seconds{model,upstream_type}`, `gw_policy_decisions_total{policy,decision}`, `gw_api_key_verify_total{source,result}`, `gw_events_published_total{type}`, `gw_events_dropped_total{reason}` |
| control planes | `cp_auth_logins_total{result}`, `cp_auth_lockouts_total` |
| rate limiters | `rl_{rpm,tpm}_decisions_total{decision}`, `rl_{rpm,tpm}_valkey_errors_total{op}`, `rl_{rpm,tpm}_consumer_active` |
| tokenizer | `tokenizer_requests_total`, `tokenizer_tokens_total{tokenizer}` |
| enricher | `enricher_events_total{source,reason}`, `enricher_process_seconds`, `enricher_build_errors_total`, `enricher_republish_failures_total`, `enricher_tokenizer_fallback_total`, `enricher_consumer_active` |
| billing | `billing_usage_persisted_total`, `billing_stripe_reported_total{outcome}`, `billing_stripe_customer_resolved_total{outcome}`, `billing_stripe_webhooks_total{event_type,outcome}`, `billing_outbox_pending`, `billing_outbox_oldest_pending_seconds`, `billing_consumer_active`, `billing_reporter_active` |

Labels are kept to a fixed, small set of values:

- The gateway's `model` is a `MODEL_ROUTES` key or `default`, never the
  model string the client sent.
- `policy` is the policy endpoint's host.
- Stripe event types outside the handled set are folded into `other`.

## Dashboards

`Observability` provisions seven dashboards from
`multistack/observability/dashboards/` into a **Multistack** folder in
Grafana:

- Services
- Model gateway
- Event pipeline
- Control planes
- Billing
- vLLM
- Data plane

Each is a ConfigMap with the `grafana_dashboard: "1"` label, which Grafana's
dashboard sidecar picks up. To turn them off, set
`KubePrometheusStackOptions(dashboards=False)`.

To reach Grafana:

```bash
kubectl --kubeconfig ~/.multistack/kubeconfig -n monitoring \
  port-forward svc/kube-prometheus-stack-grafana 3000:80
```

The admin password is in Vault at `observability/grafana`, and on the spec
after `create()`. The `route` stage can also publish Grafana at
`MULTISTACK_GRAFANA_HOST`.

## Alerts

`multistack/observability/rules/multistack.yaml` is installed as the
`multistack` PrometheusRule, labelled for this release's Prometheus.
`KubePrometheusStackOptions(alert_rules=False)` turns it off. Each rule
carries `severity: critical` or `severity: warning`.

| Group | Alerts |
|---|---|
| services | `MultistackServiceHighErrorRate` (more than 5% 5xx), `MultistackServiceSlow` (p95 above 1s; the gateway is excluded) |
| gateway | `GatewayUpstreamErrors`, `GatewayKeyVerificationUnavailable`, `GatewayPolicyUnavailable`, `GatewayDroppingEvents` |
| pipeline | `EventConsumerNotBound`, `RateLimiterFailingOpen`, `EnricherDroppingEvents`, `EnricherRepublishFailing`, `NatsConsumerBacklog` |
| billing | `BillingOutboxStale`, `BillingStripeTerminalFailures`, `BillingWebhookSignatureFailures` |
| auth | `ControlPlaneLockoutSpike` |
| data | `ValkeyDown`, `PostgresCollectorDown`, `PostgresReplicationLag`, `LonghornVolumeDegraded`, `LonghornVolumeFaulted` |
| inference | `VLLMQueueBacklog`, `VLLMKVCacheFull` |

kube-prometheus-stack's own cluster alerts (TargetDown, node, kubelet and
so on) are installed alongside these.

### Sending alerts somewhere

Without configuration, Alertmanager routes everything to a null receiver.
Alerts still fire and show in its UI, but go nowhere. To route them, put the
Alertmanager config in a Secret and name it on the spec. Receiver configs
hold webhook URLs and keys, so they never go into Helm values.

```bash
kubectl -n monitoring create secret generic alertmanager-config \
  --from-file=alertmanager.yaml=./alertmanager.yaml
```

```python
Observability(..., alertmanager_config_secret="alertmanager-config")
# full_stack.py: MULTISTACK_ALERTMANAGER_CONFIG_SECRET=alertmanager-config
```

`check_prerequisites()` refuses to install if the Secret is missing or has
no `alertmanager.yaml` key.

## Checking it works

```bash
# Every target should be up.
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090
open http://localhost:9090/targets

# Monitors and rules exist and carry the release label.
kubectl get servicemonitors,podmonitors -A -l release=kube-prometheus-stack
kubectl -n monitoring get prometheusrule multistack

# A service's own metrics.
kubectl -n gateway port-forward svc/model-gateway 8080 && curl -s localhost:8080/metrics | grep http_requests_total
```

## Changing things

- **A new metric:** define it at module level next to the code it measures,
  with a fixed, small set of label values. Add a test that reads it back
  through `prometheus_client.REGISTRY`.
- **A new service chart:** copy `templates/servicemonitor.yaml` and the
  `metrics.serviceMonitor` values block. Add a `service_monitor_labels`
  field and its `FROM_STACK` entry to the spec. `tests/test_charts.py` and
  `tests/test_driver_chart_contract.py` cover the rest.
- **Dashboards:** edit the JSON in `multistack/observability/dashboards/`.
  You can also edit in Grafana, export, and replace the file. Keep the
  `${datasource}` variable.
- **Alerts:** edit `rules/multistack.yaml`, then run
  `promtool check rules` and `promtool test rules tests/observability/rules_test.yaml`.
  CI runs both through `make test-helm`; `make install-promtool` installs
  promtool.

## Not covered yet

- GPU metrics (NVIDIA DCGM exporter, AMD ROCm device-metrics exporter).
- llama.cpp's `--metrics`.
- Logs (Loki, ROADMAP C7).
- Autoscaling on these metrics (KEDA, C5).
- The `rag` service is deliberately out of scope.
