---
name: ingress-gateway
description: Use this skill when the user's request mentions an ingress, ingress gateway, external IP, LoadBalancer, MetalLB, Istio, or how the cluster is reached from outside. Covers the real MultiStack SDK IngressGateway/MetalLBIstioOptions/IngressGatewayBackend classes.
---

# MultiStack SDK — IngressGateway / MetalLBIstioOptions / IngressGatewayBackend

Real class signatures (from `multistack.ingress_gateway` — **not** re-exported
at the top level, unlike `Gateway`/`Policy`/`Storage`; always import from
`multistack.ingress_gateway` directly). IngressGateway installs *into an
existing cluster* — it does not create one. "metallb_istio" is the only
supported implementation today.

`IngressGateway`/`MetalLBIstioOptions` are Pydantic models, and
`build_ingress_gateway_plan`'s `gateway` parameter is typed as
`IngressGateway` directly — the field-by-field shape below is generated
straight from these classes for the tool schema a model actually receives,
not retyped by hand.

```python
class MetalLBIstioOptions(BaseModel):
    metallb_release_name: str = "metallb"
    metallb_chart: str = "metallb"
    metallb_chart_version: Optional[str] = None
    metallb_namespace: str = "metallb-system"
    istio_base_release_name: str = "istio-base"
    istio_base_chart: str = "base"
    istiod_release_name: str = "istiod"
    istiod_chart: str = "istiod"
    istio_chart_version: Optional[str] = None   # one version covers base/istiod/gateway -- Istio doesn't support mixing
    istio_namespace: str = "istio-system"
    ingress_release_name: str = "istio-ingressgateway"
    ingress_chart: str = "gateway"
    ingress_namespace: str = "istio-ingress"

class IngressGateway(BaseModel):
    type: str = "metallb_istio"
    kubeconfig_path: str             # required -- the EXISTING cluster this installs onto
    options: Optional[MetalLBIstioOptions] = None   # auto-filled from `type` if left unset

    # LAN IP ranges MetalLB may hand to LoadBalancer Services. Required,
    # and always a real fact about the network -- never invent one.
    # Each entry is either "start_ip-end_ip" or a CIDR like "192.168.1.0/24".
    address_pool: list[str]

    # Which nodes MetalLB may elect to announce the pool from. Empty
    # means any node -- see the ARP rule below for when that silently
    # breaks. e.g. {"kubernetes.io/hostname": "rke2-wrk-2"}
    node_selector: Dict[str, str] = {}

    # Only known once MetalLB actually assigns an address -- set by the
    # backend's create(), never computed. Always None on a freshly built spec.
    external_endpoint: Optional[str] = None

    def validate(self) -> None: ...   # raises ValueError on:
        # - address_pool empty (nothing for MetalLB to hand out -- every LoadBalancer, including the ingress itself, stays <pending> forever)
        # - an address_pool entry with no "-" and no "/" (not recognizable as a range or CIDR)

class IngressGatewayBackend:
    def create(self, gateway: IngressGateway) -> str: ...   # returns the real external endpoint once MetalLB assigns one
    def delete(self, gateway: IngressGateway) -> None: ...
    def check_prerequisites(self, gateway: IngressGateway) -> list[str]: ...
```

Rules when generating code:
- `kubeconfig_path` is required and has no ambient fallback — the SDK deliberately never reads `$KUBECONFIG`/`~/.kube/config`, so a stale default can't silently target the wrong cluster.
- `build_ingress_gateway_plan` is for adding an ingress to a cluster that **already exists** — it needs a real `kubeconfig_path`. If the cluster is also being created in this same request, use `build_full_stack_plan` instead.
- IngressGateway depends on a cluster (`REQUIRES = ("cluster",)`) only — nothing else. Don't propose it before a cluster exists, but it needs no other layer (not storage, not MinIO).
- `address_pool` is always a real fact about the network — **never invent an IP range**. If the user hasn't given one, ask; a wrong range means the ingress gateway itself can never get an address and stays `<pending>` forever.
- **Unlike Gateway/Policy, this tool cannot return a real endpoint value.** `external_endpoint` is `None` on every freshly built spec — MetalLB only assigns a real address once the script actually runs. Never state or imply a specific IP/endpoint before that happens.
- This capability provisions the front door itself — it does **not** wire any individual service's traffic through it. Routing the Model Gateway (or anything else) through this ingress is a separate step this tool doesn't cover; don't imply otherwise.
- `node_selector` restricts which nodes MetalLB may elect to announce the pool from, and it guards a failure mode nothing reports. If the elected node has no interface on the pool's own subnet, its ARP reply never reaches that broadcast domain: the Service shows an EXTERNAL-IP, the speaker logs `serviceAnnounced`, the deploy records healthy — and every client on the LAN gets `INCOMPLETE` from its ARP table, with no error anywhere, because by Kubernetes' account nothing is wrong. Don't set it speculatively, but if the user says the address is assigned yet unreachable, or that only some nodes sit on the pool's subnet, this is the field that fixes it. A hostname (`{"kubernetes.io/hostname": "rke2-wrk-2"}`) pins one node and gives up failover; a shared label across every node on that subnet is better, and needs those nodes labelled at setup time. Either way the value is a real fact about the network — never invent a hostname or label.
- Leave `options` unset unless the user describes a specific need (pinning a chart version, a different namespace for MetalLB/Istio) — it auto-fills from `type` with working defaults otherwise.
