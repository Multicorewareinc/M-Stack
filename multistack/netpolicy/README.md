# Network Policy

Kubernetes `NetworkPolicy` support for the SDK's own services. Every field this touches is empty
by default, which means "no policy installed" — the same ADR-006 shape as
`policy.endpoints` or `event_backbone_url` on the gateway: a feature
attaches by config, and an existing deployment is untouched until a
caller opts in.

Every policy here is ingress-only — it restricts who can call in, and
leaves egress unrestricted — and every allow-list defaults empty; only
the port is ever filled in automatically, from the service's own config.
A caller always supplies the real selector for their own deployment.

`multistack/netpolicy/manifest.py` holds the two things that are actually
shared:

- `network_policy_manifest()` — a plain dict shaped like the k8s API
  (same style as `Inference.manifests()`).
- `apply_network_policy()` — that dict plus `kube.apply()` in one call,
  for a workload with no chart/template of ours to attach it to (what
  `MinIOBackend` uses).

Everything else — which services support it, what field to set, how it's
applied — lives on each capability itself, described below.

## Where it applies

| Capability | Spec field | Selects | Port | Applied via |
|---|---|---|---|---|
| `Inference` (vLLM) | `allowed_client_labels` | `{"app": <name>}` | `inference.port` | Python dict in `manifests()`, applied with the rest of the Deployment/Service |
| `Gateway` (model-gateway) | `network_policy_allowed_ingress` | chart's `selectorLabels` | `containerPort` | Helm chart template (`chart/templates/networkpolicy.yaml`), via `_values()` |
| `Policy` (rate-limiter rpm/tpm) | `network_policy_allowed_ingress` | chart's `selectorLabels` | `containerPort` | Helm chart template, via `_values()` |
| `MinIOTenant` | `network_policy_allowed_ingress` | `{"v1.min.io/tenant": <name>}` | 443 or 80 (from `request_auto_cert`) | Standalone `kubectl apply` in `MinIOBackend.create()` |

MinIO is the odd one out: its chart (`minio/tenant`) is a third-party
community chart, not ours, so there's no template to add a resource to.
`NetworkPolicy` is a separate namespaced object that only needs to know a
target's pod labels — it doesn't need cooperation from whatever chart
created those pods — so `MinIOBackend` applies it directly with
`multistack.kube.apply`, the same primitive `Inference` uses. It's also
the one case cleanup needs a manual step: `MinIOBackend.delete()`
explicitly `kubectl delete networkpolicy`s it, since it isn't part of the
Helm release and `helm uninstall` won't touch it.

## Usage

### vLLM — lock inference to the gateway only

```python
inference = Inference(
    kubeconfig_path=kc,
    name="vllm",
    # model-gateway's chart labels its pods app.kubernetes.io/name, never
    # a bare "app" key -- see "Finding the real selector" below.
    allowed_client_labels=[{"app.kubernetes.io/name": "model-gateway"}],
)
```

### model-gateway — lock it to the ingress gateway only

```python
gateway = Gateway(
    kubeconfig_path=kc,
    upstream_url=...,
    api_key_secret=...,
    # Illustrative only -- the istio `gateway` chart's actual pod labels
    # are not set or verified anywhere in this repo (no nameOverride is
    # passed in multistack/ingress_gateway/drivers/metallb_istio.py, and
    # there's no VirtualService/Gateway CR here either). Confirm the real
    # label with `kubectl get pods --show-labels` before using this.
    network_policy_allowed_ingress=[{"istio": "ingressgateway"}],
)
```

### rate-limiter rpm/tpm — lock to model-gateway only

```python
policy = Policy(
    type="rpm",
    kubeconfig_path=kc,
    cache_url=...,
    network_policy_allowed_ingress=[{"app.kubernetes.io/name": "model-gateway"}],
)
```

### MinIO — lock the tenant to whatever mirrors weights from it

```python
tenant = MinIOTenant(
    kubeconfig_path=kc,
    name="models",
    network_policy_allowed_ingress=[{"app": "vllm"}],
)
```

In every case, leaving the field unset (or `[]`) deploys exactly as it
did before this feature existed.

## Finding the real selector

A guessed label is worse than none — it either matches nothing (silent,
looks configured, isn't) or matches too much. Before setting one, confirm
it against the actual cluster:

```
kubectl --kubeconfig <kc> -n <namespace> get pods --show-labels
```

For the SDK's own charts (model-gateway, rate-limiter rpm/tpm) the
selector is `app.kubernetes.io/name: <chart name>` plus
`app.kubernetes.io/instance: <release name>` — the release name is
whatever `options.release_name` was set to, not fixed. For vLLM it's
`app: <inference.name>`. For MinIO it's `v1.min.io/tenant: <tenant.name>`,
set by the operator itself.

## Not covered, and why

| Gap | Reason |
|---|---|
| tokenizer, admin-control-plane, organization-control-plane | No chart or driver exists in this repo — they're deployed from a gitignored `scratch/` directory, and a real chart for at least two of them is reportedly being built elsewhere (cluster's `chart-test` namespace). Not this SDK's manifest to add labels to or guess at. |
| Postgres, NATS | Nothing in this repo deploys either one; same reason as above. |
| Istio ingress gateway → model-gateway | Access control at the mesh edge is Istio's own `AuthorizationPolicy`, a different resource from `NetworkPolicy` — out of scope here, and there's no `VirtualService`/`Gateway` CR in-repo yet to attach one to regardless. |
| Egress (all services) | See "Why ingress-only" above. |
| Longhorn | Nothing in the traffic matrix talks to it over the network — app pods consume it via PVC/CSI, not HTTP — so there's no app-facing edge to restrict. |

### Getting tokenizer / admin-control-plane / organization-control-plane ready

These three can't be wired up now because their manifest isn't in this
repo — but the moment a chart or driver for one of them lands, hooking it
in is small, *if* it carries a predictable label. Recommended convention,
matching what the SDK's own charts already do
(`app.kubernetes.io/name: <chart name>`):

| Service | Suggested selector |
|---|---|
| tokenizer | `app.kubernetes.io/name: tokenizer` |
| admin-control-plane | `app.kubernetes.io/name: admin-control-plane` |
| organization-control-plane | `app.kubernetes.io/name: organization-control-plane` |

Once a service's pods carry that label, whoever owns its driver has two
options, same as everything else here:

- **Own chart** — add a `networkPolicy` values block + template, same as
  `model-gateway`'s (`api/microservices/model-gateway/chart/templates/networkpolicy.yaml`).
- **No chart yet / third-party** — call
  `multistack.netpolicy.apply_network_policy()` directly, same as
  `MinIOBackend._apply_network_policy()` does.

Neither path requires touching this package again — the shared pieces
(`network_policy_manifest`, `apply_network_policy`) already exist; only
the calling driver is new.
