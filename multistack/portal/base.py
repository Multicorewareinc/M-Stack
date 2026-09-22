"""The portal driver contract, and the errors a caller can catch."""
from __future__ import annotations

from typing import List, Protocol, runtime_checkable

from ..backends.transport import NodeCommandError, NodePrerequisiteError
from .spec import Portal


class PortalError(NodeCommandError):
    """Something went wrong provisioning a portal."""


class PortalPrerequisiteError(NodePrerequisiteError):
    """The cluster isn't ready for a portal. Distinct from `PortalError`
    because the fix is not in the spec."""


@runtime_checkable
class PortalDriver(Protocol):
    """What every portal implementation must provide."""

    def check_prerequisites(self, portal: Portal) -> List[str]:
        ...

    def create(self, portal: Portal) -> str:
        ...

    def delete(self, portal: Portal) -> None:
        ...


__all__ = ["PortalDriver", "PortalError", "PortalPrerequisiteError"]
