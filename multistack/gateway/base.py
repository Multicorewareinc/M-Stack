"""What a gateway implementation must provide, and what it can raise."""
from typing import List, Protocol, runtime_checkable

from ..backends.transport import NodeCommandError, NodePrerequisiteError
from .spec import Gateway


class GatewayError(NodeCommandError):
    """Any gateway failure, catchable without naming an implementation."""


class GatewayPrerequisiteError(NodePrerequisiteError):
    """Something on the cluster is missing; the fix is not in the spec."""


@runtime_checkable
class GatewayDriver(Protocol):
    def check_prerequisites(self, gateway: Gateway) -> List[str]: ...

    def create(self, gateway: Gateway) -> str:
        """Installs it and returns the in-cluster endpoint."""

    def delete(self, gateway: Gateway) -> None: ...
