"""The enricher driver contract, and the errors a caller can catch."""
from __future__ import annotations

from typing import List, Protocol, runtime_checkable

from ..backends.transport import NodeCommandError, NodePrerequisiteError
from .spec import Enricher


class EnricherError(NodeCommandError):
    """Something went wrong provisioning the enricher."""


class EnricherPrerequisiteError(NodePrerequisiteError):
    """The cluster isn't ready: no helm on PATH, or an unreachable API
    server. Distinct from `EnricherError` because the fix is not in the
    spec."""


@runtime_checkable
class EnricherDriver(Protocol):
    """What every enricher implementation must provide."""

    def check_prerequisites(self, enricher: Enricher) -> List[str]:
        ...

    def create(self, enricher: Enricher) -> str:
        ...

    def delete(self, enricher: Enricher) -> None:
        ...


__all__ = [
    "EnricherDriver",
    "EnricherError",
    "EnricherPrerequisiteError",
]
