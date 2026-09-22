"""The route capability: one service, reachable through the front door.

    Route(kubeconfig_path=kc, name="model-gateway", namespace="platform",
          service="gateway-model-gateway", port=8080, path_prefix="/v1")

`ingress_gateway` stands the front door up — MetalLB hands a LAN address
to the Istio ingress gateway — and deliberately stops there. Its own
docstring says routing an individual service through it "is a later
capability, not this one". This is that capability.

Two implementations, and the choice is not cosmetic:

  httproute        gateway.networking.k8s.io/v1. The Kubernetes-standard
                   successor to Ingress, and the reason this capability
                   exists in a project called Multistack: the same spec
                   renders against Istio, Cilium, NGINX Gateway Fabric or
                   Envoy Gateway.
  virtualservice   networking.istio.io/v1. Istio-only, no CRDs to install
                   beyond Istio itself, and the way to reach Istio
                   features Gateway API does not cover yet.

`httproute` is the default for the portability reason above. A caller
that needs Istio-specific traffic management names the other one.

Like CNPG, this capability has two objects with different lifecycles:
the parent Gateway is the front door's routing object and wants creating
once, while a Route is per-service and created many times. They are not
two methods, though -- `create()` applies the parent only if it is
absent, then the route, so a caller never has to decide which call to
make and the second service through the door pays one extra `kubectl
get`. Folding the *fields* together is what is avoided: without
`parent_name`/`parent_namespace` every service would redeclare the front
door.
"""
from typing import ClassVar, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..capability import CapabilitySpec

SUPPORTED_TYPES: Tuple[str, ...] = ("httproute", "virtualservice")

# Where `ingress_gateway` puts the Istio ingress gateway, and what its
# Helm release names the Service. A parent Gateway binds to this rather
# than provisioning a second gateway and taking a second address from a
# MetalLB pool that is usually one or two addresses wide.
DEFAULT_PARENT_NAMESPACE = "istio-ingress"
DEFAULT_INGRESS_SERVICE = "istio-ingressgateway"
DEFAULT_PARENT_NAME = "platform-gw"


class HTTPRouteOptions(BaseModel):
    """Gateway API settings."""

    model_config = ConfigDict(extra="forbid")

    # The GatewayClass the parent Gateway asks for. "istio" is what
    # istiod registers; another implementation registers its own.
    gateway_class_name: str = "istio"

    # PathPrefix or Exact. RegularExpression is implementation-specific
    # and not part of the standard channel, so it is not offered here.
    path_match_type: str = "PathPrefix"

    # Listener port on the parent Gateway.
    listener_port: int = Field(default=80, gt=0, lt=65536)

    def validate(self) -> None:
        if self.path_match_type not in ("PathPrefix", "Exact"):
            raise ValueError(
                f"path_match_type {self.path_match_type!r} is not a Gateway "
                "API match type — use 'PathPrefix' or 'Exact'."
            )


class VirtualServiceOptions(BaseModel):
    """Istio settings."""

    model_config = ConfigDict(extra="forbid")

    # Istio's Gateway is a different object from Gateway API's, with the
    # same name. This one is selected by label, not bound by address.
    gateway_selector: Dict[str, str] = Field(
        default_factory=lambda: {"istio": "ingressgateway"}
    )
    listener_port: int = Field(default=80, gt=0, lt=65536)

    def validate(self) -> None:
        if not self.gateway_selector:
            raise ValueError(
                "gateway_selector is empty — an Istio Gateway with no "
                "selector binds to no ingress gateway and silently routes "
                "nothing."
            )


class Route(CapabilitySpec):
    """One service, reachable from outside the cluster."""

    CAPABILITY: ClassVar[str] = "route"
    SUPPORTED_TYPES: ClassVar[Tuple[str, ...]] = SUPPORTED_TYPES
    OPTIONS_FOR_TYPE: ClassVar[Dict[str, type]] = {
        "httproute": HTTPRouteOptions,
        "virtualservice": VirtualServiceOptions,
    }
    # No DEFAULT_NAMESPACES: a route belongs in the namespace of the
    # Service it points at, which no default can know. `namespace` is
    # required for that reason, unlike every other capability here.
    REQUIRES: ClassVar[Tuple[str, ...]] = ("cluster", "ingress_gateway")

    FROM_STACK: ClassVar[Dict[str, str]] = {
        "kubeconfig_path": "kubeconfig_path",
        "ingress_address": "ingress_gateway_endpoint",
    }
    PROVIDES: ClassVar[Dict[str, str]] = {"route_url": "url"}

    type: str = "httproute"
    kubeconfig_path: str
    options: Optional[object] = None

    # The route object's own name, and the namespace it lives in --
    # which must be the Service's namespace, since a route's backendRef
    # is namespace-local unless a ReferenceGrant says otherwise.
    name: str
    namespace: str

    # What traffic is sent to.
    service: str
    port: int = Field(gt=0, lt=65536)

    # Host-based routing. Empty means "any host", which is what you want
    # on a bare LAN address with no DNS -- and what you must move off
    # before two services can share port 80.
    hostnames: List[str] = Field(default_factory=list)

    path_prefix: str = "/"

    # The parent this attaches to. One front door, many routes.
    parent_name: str = DEFAULT_PARENT_NAME
    parent_namespace: str = DEFAULT_PARENT_NAMESPACE

    # The already-provisioned ingress Service the parent binds to.
    # Published by `ingress_gateway`; the Stack fills it in.
    ingress_service: str = DEFAULT_INGRESS_SERVICE
    ingress_address: Optional[str] = None

    @field_validator("path_prefix")
    @classmethod
    def _path_is_absolute(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError(
                f"path_prefix {value!r} must start with '/' — a relative "
                "path matches nothing and fails silently at the gateway "
                "rather than at admission."
            )
        return value

    @field_validator("hostnames")
    @classmethod
    def _hostnames_are_not_urls(cls, value: List[str]) -> List[str]:
        for host in value:
            if "://" in host or "/" in host or ":" in host:
                raise ValueError(
                    f"hostname {host!r} looks like a URL — give the host "
                    "alone, with no scheme, port or path."
                )
        return value

    def validate(self) -> None:
        self.validate_capability()

        if not self.namespace or not self.namespace.strip():
            raise ValueError(
                "namespace is required: a route must live in the same "
                "namespace as the Service it points at."
            )
        if not self.service.strip():
            raise ValueError("service is required — a route needs a backend.")

    @property
    def url(self) -> str:
        """Where this is reachable from outside, once applied.

        Prefers a hostname, because that is what a user will actually
        type. Falls back to the ingress address, which is what there is
        on a LAN with no DNS -- and to a placeholder when neither is
        known yet, so a caller printing this before `ingress_gateway`
        has published its address gets something readable rather than
        'http://None/'.
        """
        port = getattr(self.options, "listener_port", 80)
        host = (
            self.hostnames[0]
            if self.hostnames
            else (self.ingress_address or "<ingress-address>")
        )
        authority = host if port == 80 else f"{host}:{port}"
        return f"http://{authority}{self.path_prefix}"
