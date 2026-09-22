"""The queue driver contract, and the errors a caller can catch."""
from __future__ import annotations

from typing import List, Protocol, runtime_checkable

from ..backends.transport import NodeCommandError, NodePrerequisiteError
from .spec import Queue


class QueueError(NodeCommandError):
    """Something went wrong provisioning the queue."""


class QueuePrerequisiteError(NodePrerequisiteError):
    """The cluster isn't ready: no helm on PATH, no StorageClass, or an
    unreachable API server. Distinct from `QueueError` because the fix
    is not in the spec."""


@runtime_checkable
class QueueDriver(Protocol):
    """What every queue implementation must provide."""

    def check_prerequisites(self, queue: Queue) -> List[str]:
        ...

    def create(self, queue: Queue) -> str:
        ...

    def update(self, queue: Queue) -> str:
        ...

    def delete(self, queue: Queue) -> None:
        ...


__all__ = [
    "QueueDriver",
    "QueueError",
    "QueuePrerequisiteError",
]
