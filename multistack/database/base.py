"""What a database implementation must provide, and what it can raise."""
from typing import Any, Dict, Protocol, runtime_checkable

from ..backends.transport import NodeCommandError, NodePrerequisiteError
from ..helm import HelmRelease
from .spec import Database


class DatabaseError(NodeCommandError):
    """Any database failure, catchable without naming an implementation."""


class DatabasePrerequisiteError(NodePrerequisiteError):
    """The cluster cannot support this database; the fix is not in the spec."""


class DatabaseClusterNotFoundError(DatabaseError):
    """The database cluster this spec names does not exist."""


class DatabaseTimeoutError(DatabaseError):
    """An operation did not finish inside the spec's own deadline."""


@runtime_checkable
class DatabaseDriver(Protocol):
    """Two lifecycles, so two sets of methods.

    The operator is installable with no database on top of it, and the
    database cluster requires an operator already installed. A driver
    for an implementation that has no such split still implements both
    -- its operator half is whatever it has to put in place first.
    """

    # -- operator ------------------------------------------------------
    def check_prerequisites(self, database: Database) -> None: ...

    def create(self, database: Database) -> HelmRelease: ...

    def update(self, database: Database) -> HelmRelease: ...

    def delete(self, database: Database, **kwargs) -> None: ...

    # -- the database cluster itself -----------------------------------
    def check_cluster_prerequisites(
        self, database: Database, *, bootstrap: bool = True
    ) -> None: ...

    def create_cluster(self, database: Database, **kwargs) -> Database: ...

    def update_cluster(self, database: Database, **kwargs) -> Database: ...

    def delete_cluster(self, database: Database, **kwargs) -> None: ...

    def cluster_exists(self, database: Database) -> bool: ...

    def cluster_status(self, database: Database) -> Dict[str, Any]: ...

    def wait_for_cluster_ready(self, database: Database) -> None: ...

    def wait_for_cluster_deleted(self, database: Database) -> None: ...
