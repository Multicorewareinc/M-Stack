"""Which implementation serves which `type`, and the backend that dispatches.

Entries are `(module_path, class_name)` rather than imported classes, so a
driver's module — and everything it imports — loads only when that `type`
is actually chosen. `CapabilityBackend.driver_for()` does the resolving;
see `multistack/capability.py`.
"""
from __future__ import annotations

from typing import List

from ..capability import CapabilityBackend
from ..state.tracking import track_create, track_delete
from .spec import Observability

# type -> (module, class). Keep in step with spec.SUPPORTED_TYPES and
# spec.OPTIONS_FOR_TYPE; tests/test_capability.py enforces it, and also
# resolves every entry so a typo here fails a test rather than a deploy.
DRIVERS = {
    "kube_prometheus_stack": (
        "multistack.observability.drivers.kube_prometheus_stack",
        "KubePrometheusStackDriver",
    ),
}


class ObservabilityBackend(CapabilityBackend):
    """Provisions cluster observability, dispatching on the spec's `type`.

    Everything generic — validation, dependency checks, driver resolution,
    caching — comes from `CapabilityBackend`. All that remains is the
    capability's own method surface, one line each.
    """

    CAPABILITY = "observability"
    DRIVERS = DRIVERS

    def check_prerequisites(self, observability: Observability) -> List[str]:
        return self.driver_for(observability).check_prerequisites(observability)

    @track_create("observability", name_of=lambda observability: observability.type)
    def create(self, observability: Observability) -> str:
        return self.driver_for(observability).create(observability)

    @track_delete(name_of=lambda observability: observability.type)
    def delete(self, observability: Observability) -> None:
        return self.driver_for(observability).delete(observability)


__all__ = ["DRIVERS", "ObservabilityBackend"]
