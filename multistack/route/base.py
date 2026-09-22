"""What a route implementation must provide, and what it can raise."""
from typing import List, Protocol, runtime_checkable

from ..backends.transport import NodeCommandError, NodePrerequisiteError
from .spec import Route


class RouteError(NodeCommandError):
    """Any route failure, catchable without naming an implementation."""


class RoutePrerequisiteError(NodePrerequisiteError):
    """Something the route needs is missing -- a CRD, a parent Gateway,
    the ingress Service itself -- and the fix is not in the spec."""


@runtime_checkable
class RouteDriver(Protocol):
    def check_prerequisites(self, route: Route) -> List[str]: ...

    def create(self, route: Route) -> str:
        """Applies the parent (if not already present) and the route
        itself. Returns `route.url`."""

    def delete(self, route: Route) -> None: ...
