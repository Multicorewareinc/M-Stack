"""The control plane driver contract, and the errors a caller can catch."""
from __future__ import annotations

from typing import List, Protocol, runtime_checkable

from ..backends.transport import NodeCommandError, NodePrerequisiteError
from .spec import ControlPlane


class ControlPlaneError(NodeCommandError):
    """Something went wrong provisioning a control plane."""


class ControlPlanePrerequisiteError(NodePrerequisiteError):
    """The cluster isn't ready: no helm on PATH, an unreachable API
    server, or the Secret this references does not exist. Distinct from
    `ControlPlaneError` because the fix is not in the spec."""


@runtime_checkable
class ControlPlaneDriver(Protocol):
    """What every control plane implementation must provide."""

    def check_prerequisites(self, control_plane: ControlPlane) -> List[str]:
        ...

    def create(self, control_plane: ControlPlane) -> str:
        ...

    def delete(self, control_plane: ControlPlane) -> None:
        ...


__all__ = [
    "ControlPlaneDriver",
    "ControlPlaneError",
    "ControlPlanePrerequisiteError",
]
