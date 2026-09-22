"""The database capability: a SQL database something else connects to.

    Database(kubeconfig_path=kc, name="admin-pg",
             database=DatabaseConfig(name="app", owner="admin",
                                     password=prompt_secret(...)))

`cnpg` (CloudNativePG) is the only implementation today. The capability
is named for what a dependent asks for rather than what provides it --
`stack.py` has recorded this as `database` since before there was a
capability to go with it, and a control plane's `database_url` reads
that key, not a CNPG.

Migrated from the `core/` + `backends/` split (`core/cnpg.py`,
`backends/cnpg_client.py`), which is why the spec class is `Database`
rather than `CNPG`: a spec names the capability, an implementation is
named by `type`, the same way `Storage(type="longhorn")` and
`Cache(type="valkey")` do.

Two lifecycles, deliberately kept apart
---------------------------------------
One spec drives both, and which one a caller means is the method they
call, not a second object:

    operator   DatabaseBackend.create/update/delete -- a Helm release,
               installable with no database on top of it at all.
    cluster    DatabaseBackend.create_cluster/update_cluster/
               delete_cluster -- the PostgreSQL Cluster resource, which
               requires the operator already installed.

So the cluster fields are optional: a spec carrying only
`kubeconfig_path` is a complete, valid operator-only spec, and
`validate_cluster()` is what the cluster paths call on top of
`validate()`.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from ..capability import CapabilitySpec

SUPPORTED_TYPES: Tuple[str, ...] = ("cnpg",)

DEFAULT_OPERATOR_CHART = "cloudnative-pg"
DEFAULT_OPERATOR_NAMESPACE = "cnpg-system"
DEFAULT_OPERATOR_RELEASE = "cnpg"
DEFAULT_OPERATOR_CHART_VERSION = "0.29.0"

DEFAULT_NAMESPACE = "postgres"
DEFAULT_POSTGRES_IMAGE = "ghcr.io/cloudnative-pg/postgresql:17"
DEFAULT_STORAGE_SIZE = "10Gi"

CNPG_CLUSTER_CRD = "clusters.postgresql.cnpg.io"

# CloudNativePG names three Services per cluster: -rw for the primary,
# -ro for the replicas, -r for any instance. -rw is the one a consumer
# that writes wants, and the only one worth publishing.
PRIMARY_SERVICE_SUFFIX = "-rw"
POSTGRES_PORT = 5432

# CloudNativePG emits no scheduling constraints of its own, and
# rke2-cp01 carries no taint, so without this Postgres pods are eligible
# for the control plane -- next to etcd, competing for the same disk.
# The operator chart is a plain Helm install and takes its own affinity
# through `values`; this is for the Cluster resource.
DEFAULT_CLUSTER_AFFINITY: Dict[str, Any] = {
    "nodeAffinity": {
        "requiredDuringSchedulingIgnoredDuringExecution": {
            "nodeSelectorTerms": [
                {
                    "matchExpressions": [
                        {
                            "key": "node-role.kubernetes.io/control-plane",
                            "operator": "DoesNotExist",
                        }
                    ]
                }
            ]
        }
    }
}


class CNPGOptions(BaseModel):
    """The CloudNativePG operator: its Helm release, and its CRD.

    Everything here belongs to the operator half of the capability, and
    every field of it is CloudNativePG-specific -- a second database
    implementation would install a different chart and register a
    different CRD, which is the rule for what lives in options rather
    than on the spec. The Cluster's own shape (instances, storage,
    scheduling) stays on `Database`, because that is what a caller asks
    a database for regardless of who runs it.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    operator_release_name: str = DEFAULT_OPERATOR_RELEASE
    operator_namespace: str = DEFAULT_OPERATOR_NAMESPACE
    operator_chart: str = DEFAULT_OPERATOR_CHART
    operator_chart_version: Optional[str] = DEFAULT_OPERATOR_CHART_VERSION

    # Optional Helm values used to override the CloudNativePG
    # operator chart's default values.yaml configuration.
    values: Optional[Dict[str, Any]] = None

    install_timeout: str = "15m"

    # What `create()` verifies actually registered. A chart that
    # installed without its CRD leaves every later Cluster apply failing
    # with an unhelpful "no matches for kind".
    crd_name: str = CNPG_CLUSTER_CRD

    def validate(self) -> None:
        if not self.operator_release_name:
            raise ValueError("operator_release_name is required.")

        if not self.operator_namespace:
            raise ValueError("operator_namespace is required.")

        if not self.operator_chart:
            raise ValueError("operator_chart is required.")

        if not self.crd_name:
            raise ValueError("crd_name is required.")


class DatabaseConfig(BaseModel):
    """The database and role a Cluster bootstraps with.

    Stays a top-level export rather than moving under options: what a
    database is called, who owns it and how to authenticate as them is
    not CloudNativePG's idea -- any implementation of this capability
    would need exactly these three, and `Database.endpoint` is built
    from two of them.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
    )

    name: str
    owner: str

    # SecretStr, not str: pydantic masks it in repr() and model_dump(),
    # so a spec that reaches a traceback, a debug log or a bare
    # print() shows `SecretStr('**********')` instead of the password.
    # A plain str accepted here is coerced, so callers are unchanged;
    # reading it back is deliberately not -- `.get_secret_value()` is
    # the one place the real value appears, and it greps.
    password: SecretStr

    @model_validator(mode="after")
    def _validate_on_construction(self) -> "DatabaseConfig":
        self.validate()
        return self

    def validate(self) -> None:
        if not self.name:
            raise ValueError("database name is required.")

        if not self.owner:
            raise ValueError("database owner is required.")

        if not self.password.get_secret_value():
            raise ValueError("database password is required.")


class Database(CapabilitySpec):
    """A SQL database deployed into an existing Kubernetes cluster."""

    CAPABILITY: ClassVar[str] = "database"
    SUPPORTED_TYPES: ClassVar[Tuple[str, ...]] = SUPPORTED_TYPES
    OPTIONS_FOR_TYPE: ClassVar[Dict[str, type]] = {"cnpg": CNPGOptions}
    DEFAULT_NAMESPACES: ClassVar[Dict[str, str]] = {"cnpg": DEFAULT_NAMESPACE}
    REQUIRES: ClassVar[Tuple[str, ...]] = ("cluster",)

    FROM_STACK: ClassVar[Dict[str, str]] = {
        "kubeconfig_path": "kubeconfig_path",
    }

    # What a consumer needs to reach the database. One key, like every
    # other capability: the password is not published (see `endpoint`),
    # and the Secret holding it is announced with
    # stack.provide(database_secret_name=...) the same way the object
    # store's credentials Secret is.
    PROVIDES: ClassVar[Dict[str, str]] = {"database_url": "endpoint"}

    type: str = "cnpg"
    kubeconfig_path: str
    options: Optional[CNPGOptions] = None

    # PostgreSQL Cluster configuration. These fields are optional so the
    # same model also drives operator-only lifecycle operations.
    name: Optional[str] = None
    namespace: Optional[str] = None

    instances: int = Field(default=3, ge=1)
    image: str = DEFAULT_POSTGRES_IMAGE

    storage_size: str = DEFAULT_STORAGE_SIZE
    storage_class: Optional[str] = None

    database: Optional[DatabaseConfig] = None

    ready_timeout: int = Field(default=900, gt=0)
    ready_poll_interval: int = Field(default=15, gt=0)

    # Scheduling for the Cluster's pods. Defaults to keeping them off the
    # control plane; set it to {} to place them anywhere, deliberately.
    # deepcopy, not the constant itself: a default_factory returning one
    # shared dict would let an edit on one spec reach the module-level
    # default and every spec built after it.
    affinity: Optional[Dict[str, Any]] = Field(
        default_factory=lambda: deepcopy(DEFAULT_CLUSTER_AFFINITY)
    )
    node_selector: Optional[Dict[str, str]] = None
    tolerations: Optional[List[Any]] = None

    @model_validator(mode="after")
    def _validate_on_construction(self) -> "Database":
        """Runs `validate()` at construction, and again on assignment.

        `CapabilitySpec`'s own validator runs `validate_capability()`
        only, so without this the rules below would not fire until a
        backend dispatched -- a spec could sit for minutes in a state it
        would later reject. Same shape as `Cache`'s and
        `IngressGateway`'s.
        """
        self.validate()
        return self

    def validate(self) -> None:
        self.validate_capability()

        if not self.kubeconfig_path:
            raise ValueError(
                "kubeconfig_path is required — a database deploys into an "
                "existing Kubernetes cluster and does not fall back to an "
                "ambient kubeconfig."
            )

        if self.ready_poll_interval > self.ready_timeout:
            raise ValueError(
                "ready_poll_interval cannot be greater than ready_timeout."
            )

    def validate_cluster_identity(self) -> None:
        """Validate what it takes to *name* a Cluster.

        Split from `validate_cluster` because reading or deleting one
        needs its name and namespace and nothing else. Folded together,
        `cluster_exists()` and `delete_cluster()` both demanded a
        `database` block -- so asking whether a cluster existed, or
        removing one, meant typing its password into a spec first.
        """
        self.validate()

        if not self.name:
            raise ValueError("database cluster name is required.")

        if not self.resolved_namespace:
            raise ValueError("database cluster namespace is required.")

    def validate_cluster(self) -> None:
        """Validate what it takes to *create* one, bootstrap included."""
        self.validate_cluster_identity()

        if not self.image:
            raise ValueError("image is required.")

        if not self.storage_size:
            raise ValueError("storage_size is required.")

        if self.database is None:
            raise ValueError("database configuration is required.")

        self.database.validate()

    # -- what a consumer needs to reach this ------------------------------
    @property
    def endpoint(self) -> Optional[str]:
        """In-cluster URL for the primary, with no credential in it.

        None until this spec describes a database -- the same model also
        drives operator-only operations, and `record()` skips a None
        rather than publishing a half-built URL.

        The password is deliberately absent. This is what a consumer
        puts in non-secret configuration, and CloudNativePG already
        writes the credentials to a Secret (`secret_name`); a URL
        carrying the password would copy it into every ConfigMap that
        quotes this value. A consumer that must authenticate overrides
        it from that Secret -- which is why the control-plane charts
        take DATABASE_URL from a secretRef listed after their
        configMapRef.

        `postgresql://`, not `postgresql+asyncpg://`: the driver suffix
        is the consumer's choice, and SQLAlchemy is not the only client.
        """
        if not self.name or self.database is None:
            return None

        host = (
            f"{self.name}{PRIMARY_SERVICE_SUFFIX}"
            f".{self.resolved_namespace}.svc.cluster.local"
        )
        return (
            f"postgresql://{self.database.owner}@{host}"
            f":{POSTGRES_PORT}/{self.database.name}"
        )

    @property
    def secret_name(self) -> str:
        if not self.name:
            raise ValueError(
                "database cluster name is required to determine secret name."
            )

        return f"{self.name}-app-secret"

    # -- the operator half, off the chosen implementation's options -------
    #
    # Properties rather than fields so every call site reads exactly as
    # it did before the migration, while the values themselves live
    # where the shape says they belong. Same as `Cache.chart`.
    @property
    def operator_release_name(self) -> str:
        return self.options.operator_release_name

    @property
    def operator_namespace(self) -> str:
        return self.options.operator_namespace

    @property
    def operator_chart(self) -> str:
        return self.options.operator_chart

    @property
    def operator_chart_version(self) -> Optional[str]:
        return self.options.operator_chart_version

    @property
    def values(self) -> Optional[Dict[str, Any]]:
        return self.options.values

    @property
    def install_timeout(self) -> str:
        return self.options.install_timeout

    @property
    def crd_name(self) -> str:
        return self.options.crd_name
