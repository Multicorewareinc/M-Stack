"""What a policy implementation must provide, and what it can raise."""
from typing import List, Protocol, runtime_checkable

from ..backends.transport import NodeCommandError, NodePrerequisiteError
from .spec import Policy


class PolicyError(NodeCommandError):
    """Any policy failure, catchable without naming an implementation."""


class PolicyPrerequisiteError(NodePrerequisiteError):
    """Something on the cluster is missing; the fix is not in the spec."""


@runtime_checkable
class PolicyDriver(Protocol):
    def check_prerequisites(self, policy: Policy) -> List[str]: ...

    def create(self, policy: Policy) -> str:
        """Installs it and returns the /check endpoint."""

    def delete(self, policy: Policy) -> None: ...

    def enforcing(self, policy: Policy) -> bool:
        """Whether it is actually limiting, not merely answering.

        On the contract because the two are separable and the difference
        is invisible: a pod can be Ready, answer /check, and allow
        everything because its counting consumer never bound.
        """
