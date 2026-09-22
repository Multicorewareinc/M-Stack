"""The accelerator driver contract, and the errors a caller can catch."""
from __future__ import annotations

from typing import List, Protocol, runtime_checkable

from ..backends.transport import NodeCommandError, NodePrerequisiteError
from .spec import Accelerator


class AcceleratorError(NodeCommandError):
    """Something went wrong installing accelerator support."""


class AcceleratorPrerequisiteError(NodePrerequisiteError):
    """The cluster isn't ready: no helm on PATH, an unreachable API
    server, or nodes carrying none of the labels the plugin's own
    affinity requires. Distinct from `AcceleratorError` because the fix
    is not in the spec."""


@runtime_checkable
class AcceleratorDriver(Protocol):
    """What every accelerator implementation must provide."""

    def check_prerequisites(self, accelerator: Accelerator) -> List[str]:
        """Returns warnings; raises on anything that would leave the
        accelerator unschedulable."""
        ...

    def create(self, accelerator: Accelerator) -> str:
        """Installs the implementation and returns the extended resource
        name it made schedulable."""
        ...

    def delete(self, accelerator: Accelerator) -> None:
        """Removes the installation."""
        ...


__all__ = [
    "AcceleratorDriver",
    "AcceleratorError",
    "AcceleratorPrerequisiteError",
]
