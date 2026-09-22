"""What exists, and the backend that dispatches to it."""
from typing import Any, Dict, List

from ..capability import CapabilityBackend
from ..state.tracking import track_create, track_delete
from .base import GatewayError
from .spec import Gateway

DRIVERS: Dict[str, Any] = {
    "modelgateway": ("multistack.gateway.drivers.modelgateway",
                     "ModelGatewayDriver"),
}


class GatewayBackend(CapabilityBackend):
    CAPABILITY = "gateway"
    DRIVERS = DRIVERS
    ERROR_CLS = GatewayError

    def check_prerequisites(self, gateway: Gateway) -> List[str]:
        return self.driver_for(gateway).check_prerequisites(gateway)

    @track_create("gateway", name_of=lambda gateway: gateway.type)
    def create(self, gateway: Gateway) -> str:
        return self.driver_for(gateway).create(gateway)

    @track_delete(name_of=lambda gateway: gateway.type)
    def delete(self, gateway: Gateway) -> None:
        return self.driver_for(gateway).delete(gateway)
