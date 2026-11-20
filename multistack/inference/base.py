"""What an inference implementation must provide, and what it can raise."""

from typing import Protocol, runtime_checkable

from ..backends.transport import NodeCommandError, NodePrerequisiteError
from .spec import Inference


class InferenceError(NodeCommandError):
    """Any inference failure, catchable without naming an implementation."""


class InferencePrerequisiteError(NodePrerequisiteError):
    """Something on the cluster/node is missing; the fix is not in the spec."""


@runtime_checkable
class InferenceDriver(Protocol):
    def check_prerequisites(self, inference: Inference, nodes: list | None = None) -> list[str]: ...

    def create(self, inference: Inference, nodes: list | None = None) -> str:
        """Deploys it and returns the endpoint."""

    def delete(self, inference: Inference, remove_cache: bool = False) -> None:
        """Removes the deployment. A model cache outlives it unless
        `remove_cache` -- other deployments may share it, and it holds
        gigabytes that are slow to fetch again."""

    def endpoint(self, inference: Inference) -> str:
        """Where it actually serves — may differ from `inference.endpoint`
        when host_network requires a live cluster query for the node IP."""
