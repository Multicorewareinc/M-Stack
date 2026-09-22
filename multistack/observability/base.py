"""The observability driver contract, and the errors a caller can catch.

Separate from the registry (`registry.py`) because a driver author reads
this file and never that one, and separate from any driver because both
must be importable without loading an implementation.
"""
from __future__ import annotations

from typing import List, Protocol, runtime_checkable

from ..backends.transport import NodeCommandError, NodePrerequisiteError
from .spec import Observability


class ObservabilityError(NodeCommandError):
    """Something went wrong provisioning observability.

    Every implementation's error subclasses this, so a caller catches the
    capability and never names an implementation.
    """


class ObservabilityPrerequisiteError(NodePrerequisiteError):
    """The cluster isn't ready for observability: no helm on PATH, no
    StorageClass, an unreachable API server. Distinct from
    `ObservabilityError` because the fix is not in the spec."""


@runtime_checkable
class ObservabilityDriver(Protocol):
    """What every observability implementation must provide.

    A driver never sees `type` — by the time it is called, the choice has
    been made. It only has to honour the generic fields on `Observability`
    and read its own settings from `spec.options`.
    """

    def check_prerequisites(self, observability: Observability) -> List[str]:
        """Returns warnings; raises on anything that would leave
        observability unusable."""
        ...

    def create(self, observability: Observability) -> str:
        """Installs the implementation and returns the Grafana endpoint."""
        ...

    def delete(self, observability: Observability) -> None:
        """Removes the installation. Leaves persisted metrics/dashboards
        behind unless the caller also deletes the PVCs."""
        ...


__all__ = [
    "ObservabilityDriver",
    "ObservabilityError",
    "ObservabilityPrerequisiteError",
]
