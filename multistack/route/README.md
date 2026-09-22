# Route

One service, reachable from outside the cluster. `ingress_gateway` stands
the front door up — MetalLB hands a real LAN address to the Istio ingress
gateway — and deliberately stops there (see `multistack/ingress_gateway/README.md`).
This is the capability its own docstring names as "a later capability,
not this one": wiring an individual microservice through the front door
that's already standing.

Follows the same capability shape as `multistack/gateway/` and
`multistack/ingress_gateway/` (see `multistack/capability.py`): `spec.py`
for the declarative `Route`, `base.py` for the driver contract and error
types, `registry.py` for the `type -> driver` dispatch, `drivers/` for
each implementation.

Two implementations:

| type | Object created | Needs |
|---|---|---|
| `httproute` (default) | `gateway.networking.k8s.io/v1` `Gateway` + `HTTPRoute` | The Gateway API CRDs — not installed by RKE2 or any chart this SDK deploys |
| `virtualservice` | `networking.istio.io/v1` `Gateway` + `VirtualService` | Nothing beyond what `ingress_gateway`'s `metallb_istio` already installs |

`httproute` is the default because it's vendor-neutral — the same spec
renders against Istio, Cilium, NGINX Gateway Fabric or Envoy Gateway,
which is the reason this capability exists in a project called
Multistack. Pick `virtualservice` when you need Istio traffic management
(fault injection, mirroring, subsets) that Gateway API doesn't expose
yet.

## Usage

A separate step the user triggers explicitly, once `ingress_gateway` is
already up — the same way `IngressGatewayBackend` itself is not called
from within `RKE2Backend`.

```python
from multistack import Route, RouteBackend

route = Route(
    kubeconfig_path="/home/ubuntu/kubeconfig.yaml",
    name="model-gateway", namespace="gateway",     # the Service's own namespace
    service="gateway-model-gateway", port=8080,
    path_prefix="/v1",
)
url = RouteBackend().create(route)   # -> "http://<ingress-address>/v1"
```

`url` reads `<ingress-address>` until `ingress_address` is filled in —
either passed explicitly, or through `Stack`, which reads it from
`ingress_gateway_endpoint` (see `FROM_STACK` below).

## Two lifecycles, like CNPG's operator and cluster

A `Route` creates two different kinds of object, on two different
schedules:

- **The parent** (`Gateway`/`gateway.networking.k8s.io` or Istio's own
  `Gateway`) is shared by every route bound to it, and is created once —
  `create()` checks for it first and only applies it if missing. It binds
  to the ingress gateway's *existing* Service by hostname
  (`istio-ingressgateway.istio-ingress.svc.cluster.local`) rather than
  provisioning a second Service and taking a second address from what is
  often a one- or two-address MetalLB pool.
- **The route itself** (`HTTPRoute`/`VirtualService`) is per-service, and
  every `create()` call applies it — a second call with the same name is
  an update, not a duplicate.

`delete()` only ever removes the route, never the parent — a driver has
no way to know whether another route still depends on it, so removing
the front door itself is left as a deliberate, separate action.

## The spec

```python
Route(
    kubeconfig_path=...,
    type="httproute",             # or "virtualservice"
    name=..., namespace=...,      # namespace must match the Service's own
    service=..., port=...,
    path_prefix="/v1",            # must start with "/"
    hostnames=[],                 # empty = match on path alone
    parent_name="platform-gw",              # shared across every Route
    parent_namespace="istio-ingress",
    ingress_service="istio-ingressgateway",  # what ingress_gateway actually installed
    options=HTTPRouteOptions(...),           # or VirtualServiceOptions(...)
)
```

`namespace` has no default (`DEFAULT_NAMESPACES` is empty for this
capability) — a route belongs wherever its Service lives, which nothing
here can guess, so it's required rather than defaulted.

**`hostnames` is not cosmetic.** An empty list means "match on path
alone", and a route with no hostname claims anything a more specific
route on the same parent doesn't — in particular, a route with
`path_prefix="/"` and no hostname is a catch-all for every request that
doesn't match something more specific. That's the right default on a
bare LAN address with no DNS. Once you have a real domain, give each
route its own `hostnames` entry — that also stops path prefixes across
different routes from having to stay disjoint.

`REQUIRES = ("cluster", "ingress_gateway")` — `ingress_gateway` is
declared but, unlike `cluster`, not independently verifiable by
`CapabilityBackend.verify_requirements()`: there's no generic way to
confirm "an ingress gateway exists" the way there is for "a StorageClass
exists". `check_prerequisites()` is the real check — it confirms the
CRDs the chosen `type` actually needs are installed, and reports the fix
rather than raising, so a caller can decide whether to proceed.

## `FROM_STACK` / `PROVIDES`

```python
FROM_STACK = {"kubeconfig_path": "kubeconfig_path",
              "ingress_address": "ingress_gateway_endpoint"}
PROVIDES   = {"route_url": "url"}
```

Inside a `Stack`, `ingress_address` fills in automatically from whatever
`ingress_gateway` published — no need to pass the LAN address around by
hand.

## State tracking

`RouteBackend.create()` is decorated with
`@track_create("route", name_of=lambda route: f"{route.namespace}/{route.name}")`
(see `multistack/state/README.md`), so a run shows up as
`httproute / route / healthy` (or `failed`, with the real error) keyed by
`namespace/name` — not just `name`, since two routes in different
namespaces are allowed to share one.

## Requirements

`kubectl` on `PATH`, wherever the SDK process runs. For `httproute`,
the Gateway API CRDs:

```bash
kubectl apply -f https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.2.1/standard-install.yaml
```

Istio (via `istiod`, already running once `ingress_gateway` is up)
auto-registers the `istio` `GatewayClass` the moment those CRDs exist —
nothing else to configure for that half.
