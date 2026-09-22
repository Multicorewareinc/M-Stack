"""
The block-storage driver contract, and the errors a caller can catch.

Separate from the registry (`registry.py`) because a driver author reads
this file and never that one, and separate from any driver because both
must be importable without loading an implementation. That matters now
that drivers load lazily: `except StorageError` has to work whether or not
the driver that would raise it has ever been imported.
"""
from __future__ import annotations

from typing import List, Optional, Protocol, runtime_checkable

from ..backends.transport import NodeCommandError, NodePrerequisiteError
from .spec import Storage


class StorageError(NodeCommandError):
    """Something went wrong provisioning block storage.

    Every implementation's error subclasses this, so a caller catches the
    capability and never names an implementation — the same property the
    spec gives, applied to the failure path.
    """


class StoragePrerequisiteError(NodePrerequisiteError):
    """A node isn't ready for block storage: a missing binary, a kernel
    module, a service that isn't running. Distinct from `StorageError`
    because the fix is on the node, not in the spec."""


@runtime_checkable
class StorageDriver(Protocol):
    """What every block-storage implementation must provide.

    A driver never sees `type` — by the time it is called, the choice has
    been made. It only has to honour the generic fields on `Storage` and
    read its own settings from `spec.options`.
    """

    def check_prerequisites(self, storage: Storage, nodes: Optional[List] = None) -> List[str]:
        """Returns warnings; raises on anything that would leave storage
        unusable."""
        ...

    def install_prerequisites(self, nodes: List) -> None:
        """Installs whatever the nodes need. Mutates them, so it is a
        separate explicit call rather than part of create()."""
        ...

    def create(self, storage: Storage, nodes: Optional[List] = None) -> str:
        """Installs the implementation and returns the StorageClass name."""
        ...

    def delete(self, storage: Storage) -> None:
        """Removes the installation. Leaves data behind."""
        ...


__all__ = [
    "StorageDriver",
    "StorageError",
    "StoragePrerequisiteError",
]
