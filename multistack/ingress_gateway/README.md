# Ingress Gateway

The cluster's one external front door: MetalLB hands a real LAN address to
a `LoadBalancer` Service on bare metal (where no cloud provider does it
for you), and the Istio ingress gateway is the Service that receives it.
Routing an individual microservice (rate limiter, model gateway, ...)
through it is a later capability — this one only stands the front door
up.

Follows the same capability shape as `multistack/gateway/` and
`multistack/policy/` (see `multistack/capability.py`): `spec.py` for the
declarative `IngressGateway`, `base.py` for the driver contract and error
types, `registry.py` for the `type -> driver` dispatch, `drivers/` for the
implementation. `metallb_istio` is the only `type` today.

Deployment goes entirely through the Helm layer
(`multistack.helm.HelmRunner`) — no raw `helm`/`kubectl` subprocess calls
of its own for the install steps.

## Usage

A separate step the user triggers explicitly — once a cluster exists and
you know which addresses MetalLB may hand out on that LAN — the same way
`multistack.backends.minio_client` is: `RKE2Backend` deploys the cluster
and nothing else; this is not called from within it.

```python
from multistack.ingress_gateway import IngressGateway, IngressGatewayBackend

gateway = IngressGateway(
    kubeconfig_path="/home/ubuntu/kubeconfig.yaml",
    address_pool=["192.168.6.91-192.168.6.91"],   # the MetalLB VIP(s) you provide
)
endpoint = IngressGatewayBackend().create(gateway)   # -> "192.168.6.91"
```

## What `create()` actually does

1. Install `metallb` (Helm, namespace `metallb-system`).
2. Apply its `IPAddressPool` + `L2Advertisement` custom resources from
   `address_pool` — L2/ARP mode, so no BGP router is required. (Waits for
   MetalLB's own `--wait`ed install first, since these CRDs don't exist
   until then.)
3. Install `istio-base`, then `istiod` (Helm, namespace `istio-system`).
4. Install the `gateway` chart as `istio-ingressgateway` (Helm, namespace
   `istio-ingress`) — this is the Service MetalLB assigns an address to.
5. Poll that Service until MetalLB assigns it an external IP, and return
   it. It's also written back onto the spec as `external_endpoint`.

`delete()` uninstalls in the reverse order and removes the two MetalLB
custom resources.

## The spec

```python
IngressGateway(
    kubeconfig_path=...,
    address_pool=["192.168.1.240-192.168.1.250"],   # or a CIDR
    options=MetalLBIstioOptions(...),  # chart/release/namespace overrides
)
```

`address_pool` is required and caller-supplied — each entry a MetalLB
range (`start-end`) or CIDR. `external_endpoint` starts `None` and is
filled in by `create()`; it's what `Stack.record()` publishes as
`ingress_gateway_endpoint` for later layers.

`REQUIRES = ("cluster",)` — needs a reachable kubeconfig, checked for real
by `require_cluster` regardless of what the state layer has recorded.

## State tracking

`IngressGatewayBackend.create()` is decorated with
`@track_create("ingress_gateway", name_of=lambda gateway: gateway.type)`
(see `multistack/state/README.md`), so a run shows up as
`metallb_istio / ingress_gateway / healthy` (or `failed`, with the real
error) in `default_state_manager().list_deployments()`.

That `REQUIRES = ("cluster",)` dependency is checked against the state
layer too (see `multistack/state/README.md`): if nothing has recorded a
healthy `"cluster"` deployment yet, `create()` raises
`DependencyResolutionError` before touching Helm at all.

## Requirements

`helm` and `kubectl` on `PATH`, wherever the SDK process runs (not
necessarily the cluster node — cluster-side commands are always scoped
with `--kubeconfig`, never ambient resolution).
