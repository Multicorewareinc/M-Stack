"""Which implementation serves which `type`, and the backend that dispatches."""
from __future__ import annotations

from typing import List

from ..capability import CapabilityBackend
from ..state.tracking import track_create, track_delete
from .spec import Billing

DRIVERS = {
    "stripe": ("multistack.billing.drivers.stripe", "StripeBillingDriver"),
}


class BillingBackend(CapabilityBackend):
    """Provisions billing, dispatching on the spec's `type`."""

    CAPABILITY = "billing"
    DRIVERS = DRIVERS

    def check_prerequisites(self, billing: Billing) -> List[str]:
        return self.driver_for(billing).check_prerequisites(billing)

    @track_create("billing", name_of=lambda billing: billing.type)
    def create(self, billing: Billing) -> str:
        return self.driver_for(billing).create(billing)

    @track_delete(name_of=lambda billing: billing.type)
    def delete(self, billing: Billing) -> None:
        return self.driver_for(billing).delete(billing)


__all__ = ["DRIVERS", "BillingBackend"]
