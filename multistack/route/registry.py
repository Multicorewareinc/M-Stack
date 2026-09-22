"""What exists, and the backend that dispatches to it."""
from typing import Any, Dict, List

from ..capability import CapabilityBackend
from ..state.tracking import track_create, track_delete
from .base import RouteError
from .spec import Route

DRIVERS: Dict[str, Any] = {
    "httproute": ("multistack.route.drivers.httproute", "HTTPRouteDriver"),
    "virtualservice": ("multistack.route.drivers.virtualservice", "VirtualServiceDriver"),
}


class RouteBackend(CapabilityBackend):
    CAPABILITY = "route"
    DRIVERS = DRIVERS
    ERROR_CLS = RouteError

    def check_prerequisites(self, route: Route) -> List[str]:
        return self.driver_for(route).check_prerequisites(route)

    @track_create("route", name_of=lambda route: f"{route.namespace}/{route.name}")
    def create(self, route: Route) -> str:
        return self.driver_for(route).create(route)

    @track_delete(name_of=lambda route: f"{route.namespace}/{route.name}")
    def delete(self, route: Route) -> None:
        return self.driver_for(route).delete(route)
