"""What exists, and the backend that dispatches to it."""
from typing import Any, Dict, List

from ..capability import CapabilityBackend
from ..state.tracking import track_create, track_delete
from .base import PolicyError
from .spec import Policy

# Named, not imported: a driver loads only when its type is selected.
DRIVERS: Dict[str, Any] = {
    "rpm": ("multistack.policy.drivers.rpm", "RPMDriver"),
    "tpm": ("multistack.policy.drivers.tpm", "TPMDriver"),
}


class PolicyBackend(CapabilityBackend):
    CAPABILITY = "policy"
    DRIVERS = DRIVERS
    ERROR_CLS = PolicyError

    def check_prerequisites(self, policy: Policy) -> List[str]:
        return self.driver_for(policy).check_prerequisites(policy)

    @track_create("policy", name_of=lambda policy: policy.type)
    def create(self, policy: Policy) -> str:
        return self.driver_for(policy).create(policy)

    @track_delete(name_of=lambda policy: policy.type)
    def delete(self, policy: Policy) -> None:
        return self.driver_for(policy).delete(policy)

    def enforcing(self, policy: Policy) -> bool:
        return self.driver_for(policy).enforcing(policy)
