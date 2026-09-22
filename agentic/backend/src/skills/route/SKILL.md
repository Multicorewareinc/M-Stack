---
name: route
description: Use this skill when the user's request mentions a route, routing traffic to a service, exposing a service externally, HTTPRoute, VirtualService, hostnames, or path prefixes. Covers the real MultiStack SDK Route/RouteBackend classes.
---

# MultiStack SDK — Route / RouteBackend

Real class signature (from `multistack`, re-exported at the top level as
`from multistack import Route, RouteBackend`). The `ingress_gateway`
capability stands the front door up — MetalLB hands a LAN address to the
Istio ingress gateway — and deliberately stops there. **This is the
capability that routes one service through it.**

```python
class Route(CapabilitySpec):
    type: str = "httproute"           # or "virtualservice" -- see rules
    kubeconfig_path: str              # required -- the EXISTING cluster
    options: Optional[HTTPRouteOptions | VirtualServiceOptions] = None

    name: str                         # required -- the routing object's own name
    namespace: str                    # required -- MUST be the Service's namespace
    service: str                      # required -- in-cluster Service NAME, not a URL
    port: int                         # required
    hostnames: List[str] = []         # bare hosts: no scheme, no port, no path
    path_prefix: str = "/"            # must start with "/"

    parent_name: str = "platform-gw"  # the front door's routing object
    parent_namespace: str = "istio-ingress"
    ingress_service: str = "istio-ingressgateway"   # the ingress Service the parent binds to
    ingress_address: Optional[str] = None   # filled from the ingress_gateway layer

    @property
    def url(self) -> str: ...         # http://<host-or-address><path_prefix>
```

Rules when generating code:
- `kubeconfig_path` is required and has no ambient fallback.
- **Requires an ingress gateway to already exist** (`REQUIRES = ("cluster", "ingress_gateway")`) — this routes through the front door, it does not create one. See the `ingress-gateway` skill.
- `namespace` **must be the namespace of the Service being routed to** — a route lives beside its backend. `service` is the in-cluster Service *name* (e.g. `gateway-model-gateway`), never a URL.
- `path_prefix` must start with `/` — a relative path matches nothing and fails silently at the gateway rather than at admission.
- `hostnames` are bare hosts: no scheme, no port, no path. The SDK rejects anything that looks like a URL.
- **`type` is not cosmetic.** `httproute` (the default) is Gateway API — the Kubernetes-standard successor to Ingress, and portable across Istio, Cilium, NGINX Gateway Fabric and Envoy Gateway. `virtualservice` is Istio-only. Keep the default unless the user asks for Istio-specific traffic management.
- **This rule is not enforced at construction** — unlike Gateway/Policy, `Route` has no automatic re-check of its namespace/service rules, so `build_route_plan` calls `validate()` explicitly rather than trusting construction.
- **The real external URL is not known until MetalLB has assigned the ingress an address.** `url` may come back containing a literal `<ingress-address>` placeholder — never present that as a real address.
- The parent Gateway and the Route have different lifecycles, but they are **one call**: `create()` applies the parent only if absent, then the route. Don't propose a separate step for it.
