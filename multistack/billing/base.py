"""The billing driver contract, and the errors a caller can catch."""
from __future__ import annotations

from typing import List, Protocol, runtime_checkable

from ..backends.transport import NodeCommandError, NodePrerequisiteError
from .spec import Billing


class BillingError(NodeCommandError):
    """Something went wrong provisioning billing."""


class BillingPrerequisiteError(NodePrerequisiteError):
    """The cluster isn't ready: no helm on PATH, an unreachable API
    server, or the Secret this references does not exist. Distinct from
    `BillingError` because the fix is not in the spec."""


@runtime_checkable
class BillingDriver(Protocol):
    """What every billing implementation must provide."""

    def check_prerequisites(self, billing: Billing) -> List[str]:
        ...

    def create(self, billing: Billing) -> str:
        ...

    def delete(self, billing: Billing) -> None:
        ...


__all__ = [
    "BillingDriver",
    "BillingError",
    "BillingPrerequisiteError",
]
