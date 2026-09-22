"""What exists, and the backend that dispatches to it."""
from typing import Any, Dict

from ..capability import CapabilityBackend
from ..helm import HelmRelease
from ..state.tracking import track_create, track_delete, track_update
from .base import DatabaseClusterNotFoundError, DatabaseError
from .spec import Database

DRIVERS: Dict[str, Any] = {
    "cnpg": ("multistack.database.drivers.cnpg", "CNPGDriver"),
}


class DatabaseBackend(CapabilityBackend):
    """Installs a database operator, and the databases that run on it.

    The two lifecycles are deliberately independent: `create()` puts the
    operator in place and stops there, `create_cluster()` needs one
    already installed. A caller that wants both calls both, in that
    order, and can see that it did.
    """

    CAPABILITY = "database"
    DRIVERS = DRIVERS
    ERROR_CLS = DatabaseError

    # ------------------------------------------------------------------
    # Operator lifecycle
    # ------------------------------------------------------------------
    def check_prerequisites(self, database: Database) -> None:
        return self.driver_for(database).check_prerequisites(database)

    # Two tracked things, because there are two public lifecycles. The
    # operator is installable with no database at all, and `delete()`
    # removes its namespace -- so it is a deployment the state layer
    # should be able to show, not an internal step. It gets a type that
    # cannot be mistaken for the database itself; "database" is what a
    # dependent's REQUIRES asks for, and only the cluster provides that.
    @track_create("database-operator",
                  name_of=lambda database: database.operator_release_name)
    def create(self, database: Database) -> HelmRelease:
        return self.driver_for(database).create(database)

    @track_update(name_of=lambda database: database.operator_release_name)
    def update(self, database: Database) -> HelmRelease:
        return self.driver_for(database).update(database)

    @track_delete(name_of=lambda database: database.operator_release_name)
    def delete(
        self,
        database: Database,
        *,
        wait: bool = True,
        remove_namespace: bool = True,
    ) -> None:
        return self.driver_for(database).delete(
            database, wait=wait, remove_namespace=remove_namespace
        )

    # ------------------------------------------------------------------
    # Database cluster lifecycle
    # ------------------------------------------------------------------
    def check_cluster_prerequisites(
        self,
        database: Database,
        *,
        bootstrap: bool = True,
    ) -> None:
        return self.driver_for(database).check_cluster_prerequisites(
            database, bootstrap=bootstrap
        )

    @track_create("database", name_of=lambda database: database.name)
    def create_cluster(
        self,
        database: Database,
        *,
        wait_for_ready: bool = True,
    ) -> Database:
        return self.driver_for(database).create_cluster(
            database, wait_for_ready=wait_for_ready
        )

    # A rejected change is not a broken database: CNPG refuses an
    # instance count below 1 and a storage shrink at admission, before
    # anything is touched, and marking the cluster failed for that would
    # report the SDK working as the deployment breaking.
    @track_update(name_of=lambda database: database.name,
                  refused=(DatabaseClusterNotFoundError,))
    def update_cluster(
        self,
        database: Database,
        *,
        wait_for_ready: bool = True,
    ) -> Database:
        return self.driver_for(database).update_cluster(
            database, wait_for_ready=wait_for_ready
        )

    @track_delete(name_of=lambda database: database.name)
    def delete_cluster(
        self,
        database: Database,
        *,
        remove_secret: bool = True,
        wait: bool = True,
    ) -> None:
        return self.driver_for(database).delete_cluster(
            database, remove_secret=remove_secret, wait=wait
        )

    # ------------------------------------------------------------------
    # Status and readiness
    # ------------------------------------------------------------------
    def cluster_exists(self, database: Database) -> bool:
        return self.driver_for(database).cluster_exists(database)

    def cluster_status(self, database: Database) -> Dict[str, Any]:
        return self.driver_for(database).cluster_status(database)

    def wait_for_cluster_ready(self, database: Database) -> None:
        return self.driver_for(database).wait_for_cluster_ready(database)

    def wait_for_cluster_deleted(self, database: Database) -> None:
        return self.driver_for(database).wait_for_cluster_deleted(database)


__all__ = ["DRIVERS", "DatabaseBackend"]
