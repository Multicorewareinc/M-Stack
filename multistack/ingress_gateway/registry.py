"""What exists, and the backend that dispatches to it."""
from typing import Any, Dict, List

from ..capability import CapabilityBackend
from ..state.tracking import track_create, track_delete
from .base import IngressGatewayError
from .spec import IngressGateway

DRIVERS: Dict[str, Any] = {
    "metallb_istio": ("multistack.ingress_gateway.drivers.metallb_istio",
                       "MetalLBIstioDriver"),
}


class IngressGatewayBackend(CapabilityBackend):
    CAPABILITY = "ingress_gateway"
    DRIVERS = DRIVERS
    ERROR_CLS = IngressGatewayError

    def check_prerequisites(self, gateway: IngressGateway) -> List[str]:
        return self.driver_for(gateway).check_prerequisites(gateway)

    @track_create("ingress_gateway", name_of=lambda gateway: gateway.type)
    def create(self, gateway: IngressGateway) -> str:
        return self.driver_for(gateway).create(gateway)

    @track_delete(name_of=lambda gateway: gateway.type)
    def delete(self, gateway: IngressGateway) -> None:
        return self.driver_for(gateway).delete(gateway)
