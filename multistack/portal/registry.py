"""Which implementation serves which `type`, and the backend that dispatches."""
from __future__ import annotations

from typing import List

from ..capability import CapabilityBackend
from ..state.tracking import track_create, track_delete
from .spec import Portal

# One chart, one driver, two portals.
_DRIVER = ("multistack.portal.drivers.nginx_spa", "NginxSPADriver")

DRIVERS = {"admin": _DRIVER, "organization": _DRIVER}


class PortalBackend(CapabilityBackend):
    """Provisions a portal, dispatching on the spec's `type`."""

    CAPABILITY = "portal"
    DRIVERS = DRIVERS

    def check_prerequisites(self, portal: Portal) -> List[str]:
        return self.driver_for(portal).check_prerequisites(portal)

    @track_create("portal", name_of=lambda portal: portal.type)
    def create(self, portal: Portal) -> str:
        return self.driver_for(portal).create(portal)

    @track_delete(name_of=lambda portal: portal.type)
    def delete(self, portal: Portal) -> None:
        return self.driver_for(portal).delete(portal)


__all__ = ["DRIVERS", "PortalBackend"]
