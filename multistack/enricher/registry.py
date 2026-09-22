"""Which implementation serves which `type`, and the backend that dispatches."""
from __future__ import annotations

from typing import List

from ..capability import CapabilityBackend
from ..state.tracking import track_create, track_delete
from .spec import Enricher

DRIVERS = {
    "enricher": ("multistack.enricher.drivers.enricher", "JetStreamEnricherDriver"),
}


class EnricherBackend(CapabilityBackend):
    """Provisions the enricher, dispatching on the spec's `type`."""

    CAPABILITY = "enricher"
    DRIVERS = DRIVERS

    def check_prerequisites(self, enricher: Enricher) -> List[str]:
        return self.driver_for(enricher).check_prerequisites(enricher)

    @track_create("enricher", name_of=lambda enricher: enricher.type)
    def create(self, enricher: Enricher) -> str:
        return self.driver_for(enricher).create(enricher)

    @track_delete(name_of=lambda enricher: enricher.type)
    def delete(self, enricher: Enricher) -> None:
        return self.driver_for(enricher).delete(enricher)


__all__ = ["DRIVERS", "EnricherBackend"]
