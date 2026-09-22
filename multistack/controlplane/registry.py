"""Which implementation serves which `type`, and the backend that dispatches."""
from __future__ import annotations

from typing import List

from ..capability import CapabilityBackend
from ..state.tracking import track_create, track_delete
from .spec import ControlPlane

# Both types share one driver: they differ in which values they read, not
# in how they are installed.
_DRIVER = ("multistack.controlplane.drivers.fastapi_service",
           "FastAPIControlPlaneDriver")

DRIVERS = {"admin": _DRIVER, "organization": _DRIVER}


class ControlPlaneBackend(CapabilityBackend):
    """Provisions a control plane, dispatching on the spec's `type`."""

    CAPABILITY = "controlplane"
    DRIVERS = DRIVERS

    def check_prerequisites(self, control_plane: ControlPlane) -> List[str]:
        return self.driver_for(control_plane).check_prerequisites(control_plane)

    @track_create("controlplane", name_of=lambda cp: cp.type)
    def create(self, control_plane: ControlPlane) -> str:
        return self.driver_for(control_plane).create(control_plane)

    @track_delete(name_of=lambda cp: cp.type)
    def delete(self, control_plane: ControlPlane) -> None:
        return self.driver_for(control_plane).delete(control_plane)


__all__ = ["DRIVERS", "ControlPlaneBackend"]
