"""Which implementation serves which `type`, and the backend that dispatches."""
from __future__ import annotations

from typing import List

from ..capability import CapabilityBackend
from ..state.tracking import track_create, track_delete
from .spec import Accelerator

DRIVERS = {
    "nvidia_device_plugin": (
        "multistack.accelerator.drivers.nvidia_device_plugin",
        "NvidiaDevicePluginDriver",
    ),
}


class AcceleratorBackend(CapabilityBackend):
    """Installs accelerator support, dispatching on the spec's `type`."""

    CAPABILITY = "accelerator"
    DRIVERS = DRIVERS

    def check_prerequisites(self, accelerator: Accelerator) -> List[str]:
        return self.driver_for(accelerator).check_prerequisites(accelerator)

    # name_of=type, like every other single-instance capability: one
    # cluster runs one device plugin per accelerator vendor, so the type
    # is the identity.
    @track_create("accelerator", name_of=lambda accelerator: accelerator.type)
    def create(self, accelerator: Accelerator) -> str:
        return self.driver_for(accelerator).create(accelerator)

    @track_delete(name_of=lambda accelerator: accelerator.type)
    def delete(self, accelerator: Accelerator) -> None:
        return self.driver_for(accelerator).delete(accelerator)


__all__ = ["DRIVERS", "AcceleratorBackend"]
