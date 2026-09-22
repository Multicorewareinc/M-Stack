"""The ingress-gateway capability: the cluster's one external front door.

    IngressGateway(kubeconfig_path=kc, address_pool=["192.168.1.240-192.168.1.250"])

MetalLB hands out real LAN IPs to `LoadBalancer` Services on bare metal,
where no cloud provider does it for you; the Istio ingress gateway is the
Service that receives one and is meant to sit in front of everything else.
`metallb_istio` is the only implementation — this capability exists so the
platform's microservices (rate limiter, model gateway, ...) each get a
route through one already-provisioned front door instead of each needing
its own LoadBalancer IP. Wiring an individual service's Gateway/
VirtualService through it is a later capability, not this one.
"""
from typing import ClassVar, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..capability import CapabilitySpec

SUPPORTED_TYPES: Tuple[str, ...] = ("metallb_istio",)


class MetalLBIstioOptions(BaseModel):
    """Chart/release/namespace settings for the first-party implementation."""

    model_config = ConfigDict(extra="forbid")

    metallb_release_name: str = "metallb"
    metallb_chart: str = "metallb"
    metallb_chart_version: Optional[str] = None
    metallb_namespace: str = "metallb-system"

    istio_base_release_name: str = "istio-base"
    istio_base_chart: str = "base"
    istiod_release_name: str = "istiod"
    istiod_chart: str = "istiod"
    # One version for base/istiod/gateway: Istio does not support mixing
    # versions across its own control-plane and gateway charts.
    istio_chart_version: Optional[str] = None
    istio_namespace: str = "istio-system"

    ingress_release_name: str = "istio-ingressgateway"
    ingress_chart: str = "gateway"
    ingress_namespace: str = "istio-ingress"

    def validate(self) -> None:
        return None


class IngressGateway(CapabilitySpec):
    """A cluster-wide external entry point: MetalLB address allocation
    fronted by the Istio ingress gateway."""

    CAPABILITY: ClassVar[str] = "ingress_gateway"
    SUPPORTED_TYPES: ClassVar[Tuple[str, ...]] = SUPPORTED_TYPES
    OPTIONS_FOR_TYPE: ClassVar[Dict[str, type]] = {
        "metallb_istio": MetalLBIstioOptions}
    REQUIRES: ClassVar[Tuple[str, ...]] = ("cluster",)

    FROM_STACK: ClassVar[Dict[str, str]] = {"kubeconfig_path": "kubeconfig_path"}
    # Only known once MetalLB assigns it, so it is recorded onto the spec
    # by create() rather than computed as a property — see driver.create().
    PROVIDES: ClassVar[Dict[str, str]] = {
        "ingress_gateway_endpoint": "external_endpoint"}

    type: str = "metallb_istio"
    kubeconfig_path: str
    options: Optional[MetalLBIstioOptions] = None

    # IP ranges MetalLB may hand to LoadBalancer Services, each either a
    # "start-end" range or a CIDR (MetalLB's own IPAddressPool syntax).
    # Required and caller-supplied: which addresses are free on the LAN is
    # a fact about the network, not something this SDK can derive.
    address_pool: List[str]

    # Labels identifying the nodes that sit on the address pool's own
    # subnet. Empty means every node, which is only right when every node
    # is in one L2 segment.
    #
    # Applied to the L2Advertisement as `nodeSelectors`, and to the
    # gateway pod as a `nodeSelector`. The first is the one that matters:
    # MetalLB elects an announcing node per service, and with the default
    # `externalTrafficPolicy: Cluster` **any** node is a candidate --
    # having the endpoint locally is not required, because kube-proxy (or
    # Cilium) forwards from wherever the packet arrives. So pinning only
    # the pod does not decide who answers ARP.
    #
    # If the elected node has no interface on the pool's subnet, its ARP
    # reply never reaches the pool's broadcast domain. The Service shows
    # an EXTERNAL-IP, the speaker logs `serviceAnnounced`, this layer
    # records healthy -- and every client on the LAN gets `INCOMPLETE`
    # from its own ARP table. Nothing anywhere reports an error, because
    # by Kubernetes' account nothing is wrong.
    #
    # That is exactly what ai-cluster did: pool 192.168.6.240-250, elected
    # announcer addressed 192.168.63.10, unreachable and green.
    #
    #   node_selector={"kubernetes.io/hostname": "rke2-wrk-2"}
    #
    # A hostname pins one node and gives up failover. A shared label
    # across every node on the subnet is better, and needs those nodes
    # labelled at setup time.
    node_selector: Dict[str, str] = Field(default_factory=dict)

    # Set by the driver once MetalLB assigns an address to the ingress
    # gateway Service; None beforehand.
    external_endpoint: Optional[str] = None

    @field_validator("address_pool")
    @classmethod
    def _pool_entries_look_like_ranges(cls, value: List[str]) -> List[str]:
        for entry in value:
            if not entry or " " in entry or ("-" not in entry and "/" not in entry):
                raise ValueError(
                    f"address_pool entry {entry!r} is not a MetalLB range — "
                    "use 'start_ip-end_ip' or a CIDR like '192.168.1.0/24'."
                )
        return value

    def validate(self) -> None:
        self.validate_capability()
        if not self.address_pool:
            raise ValueError(
                "address_pool is empty — MetalLB has no addresses to hand "
                "out, so every LoadBalancer Service (including the ingress "
                "gateway itself) would stay <pending> forever."
            )

    @model_validator(mode="after")
    def _validate_on_construction(self):
        """Runs validate() at construction, and again on any assignment.

        Without this the capability's own rules only run when the backend
        dispatches, so a spec could exist for minutes in a state it would
        later reject. `validate()` stays public because driver_for() calls
        it and it reads well at a call site — it just is not the first
        place a problem shows up.
        """
        self.validate()
        return self
