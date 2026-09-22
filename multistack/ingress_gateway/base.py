"""What an ingress-gateway implementation must provide, and what it can raise."""
from typing import List, Protocol, runtime_checkable

from ..backends.transport import NodeCommandError, NodePrerequisiteError
from .spec import IngressGateway


class IngressGatewayError(NodeCommandError):
    """Any ingress-gateway failure, catchable without naming an implementation."""


class IngressGatewayPrerequisiteError(NodePrerequisiteError):
    """Something on the cluster is missing; the fix is not in the spec."""


@runtime_checkable
class IngressGatewayDriver(Protocol):
    def check_prerequisites(self, gateway: IngressGateway) -> List[str]: ...

    def create(self, gateway: IngressGateway) -> str:
        """Installs it and returns the externally reachable endpoint."""

    def delete(self, gateway: IngressGateway) -> None: ...
