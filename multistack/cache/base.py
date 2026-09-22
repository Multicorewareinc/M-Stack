"""What a cache implementation must provide, and what it can raise."""
from typing import Protocol, runtime_checkable

from ..backends.transport import NodeCommandError, NodePrerequisiteError
from ..helm import HelmRelease
from .spec import Cache


class CacheError(NodeCommandError):
    """Any cache failure, catchable without naming an implementation."""


class CachePrerequisiteError(NodePrerequisiteError):
    """The cluster cannot support this cache; the fix is not in the spec."""


class CacheReleaseNotFoundError(CacheError):
    """The release this spec names does not exist."""


@runtime_checkable
class CacheDriver(Protocol):
    def check_prerequisites(self, cache: Cache) -> None: ...

    def create(self, cache: Cache) -> HelmRelease: ...

    def update(self, cache: Cache, **kwargs) -> HelmRelease: ...

    def delete(self, cache: Cache, **kwargs) -> None: ...

    def exists(self, cache: Cache) -> bool: ...

    def status(self, cache: Cache) -> HelmRelease: ...
