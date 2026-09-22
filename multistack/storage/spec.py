"""
Block storage: one declarative spec, several interchangeable
implementations.

    Storage(type="longhorn", kubeconfig_path=...)

`type` names the implementation. Code that consumes a `Storage` never
learns which one is in use — a caller asks for block storage, not for
Longhorn — so a second implementation can be added without changing
anything above the spec.

Fields that mean the same thing for every implementation live directly on
`Storage`. Anything specific to one lives in a typed `options` object that
must match `type`, so `kubelet_root_dir` (a Longhorn CSI quirk) cannot be
set on an implementation that has no such concept and quietly do nothing.

Object storage is a *separate* capability, not another `type` here — you
cannot back a PVC with MinIO, so the two are not substitutable and grouping
them would make the choice meaningless. Substitutability, not topic, is the
test for whether something is one capability or two.

The generic type/options/namespace handling comes from
`multistack.capability`; see that module for the recipe for adding a
capability or an implementation.
"""
from __future__ import annotations

from typing import Any, ClassVar, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..capability import CapabilitySpec

# Implementations with a working driver. The DRIVERS registry in
# __init__.py maps the same keys to drivers; the two must stay in step,
# and a test in tests/test_capability.py enforces it.
SUPPORTED_TYPES = ("longhorn",)


class LonghornOptions(BaseModel):
    """Longhorn-specific settings. The defaults are the working ones — each
    comment says what it prevents."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    release_name: str = "longhorn"
    # Decoupled from release_name (unlike before) so a local chart path or
    # a mirror can be substituted without renaming the release itself --
    # the same override `ValkeyOptions.chart`/`IngressGatewayOptions`'s
    # per-subchart fields already give every other Helm-based capability.
    chart: str = "longhorn"
    # Where Longhorn keeps replica data on each node.
    data_path: str = "/var/lib/longhorn"
    # Pinned explicitly because Longhorn's auto-detection reads kubelet's
    # cmdline via pod logs, which fails when the API server can't reach a
    # node — and then the CSI driver never deploys at all.
    kubelet_root_dir: str = "/var/lib/kubelet"
    # RWX volumes need an NFSv4 client on every node. Turn off if RWO is
    # enough and you'd rather not install nfs-common.
    enable_rwx: bool = True

    @field_validator("release_name")
    @classmethod
    def _not_empty(cls, value: str, info) -> str:
        if not value:
            raise ValueError(f"{info.field_name} must not be empty")
        return value

    @field_validator("data_path", "kubelet_root_dir")
    @classmethod
    def _absolute_path(cls, value: str, info) -> str:
        # Both become on-node paths; a relative one silently resolves
        # against whatever the kubelet's working directory happens to be.
        if not value.startswith("/"):
            raise ValueError(
                f"{info.field_name} must be an absolute path, got {value!r}"
            )
        return value

    def validate(self) -> None:
        """Kept for `Storage.validate()`, which calls it on the options
        object. Construction already validated; re-running is cheap and
        catches an options model built by other means."""
        self.model_validate(self.model_dump())


ACCESS_MODES = ("ReadWriteOnce", "ReadOnlyMany", "ReadWriteMany", "ReadWriteOncePod")


class VolumeClaim(BaseModel):
    """
    A PersistentVolumeClaim backed by whatever `Storage` provides.

    Deliberately not tied to an implementation: a claim names a
    StorageClass, and every block-storage implementation produces one. So
    this is the same object whether the class came from Longhorn or
    anything else, and `StorageBackend` implements it once rather than each
    driver reimplementing PVCs.

    `storage_class` left None means "whichever class the Storage spec
    produces", which is the usual case and the one people get wrong by
    hardcoding a name.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    name: str
    namespace: str = "default"
    size: str = "5Gi"
    access_mode: str = "ReadWriteOnce"
    storage_class: Optional[str] = None

    @field_validator("name", "namespace")
    @classmethod
    def _not_empty(cls, value: str, info) -> str:
        if not value.strip():
            raise ValueError(f"{info.field_name} must not be empty")
        return value

    @field_validator("size")
    @classmethod
    def _quantity(cls, value: str) -> str:
        # A Kubernetes quantity, not a number. "10" is 10 *bytes*, which
        # is a PVC that binds and then immediately fills.
        import re

        # The unit is required. Kubernetes reads a bare "10" as ten
        # *bytes* — a claim that binds and is instantly full — and nobody
        # sizing a volume means that.
        if not re.fullmatch(r"\d+(\.\d+)?(Ei|Pi|Ti|Gi|Mi|Ki|E|P|T|G|M|k)", value):
            raise ValueError(
                f"size must be a Kubernetes quantity with a unit, like '10Gi', "
                f"got {value!r}. A bare number is bytes."
            )
        return value

    @field_validator("access_mode")
    @classmethod
    def _known_access_mode(cls, value: str) -> str:
        if value not in ACCESS_MODES:
            raise ValueError(
                f"Unknown access_mode {value!r}. Expected one of {ACCESS_MODES}. "
                "ReadWriteMany needs an implementation that serves it — for "
                "Longhorn that means enable_rwx and an NFSv4 client on every node."
            )
        return value

    def manifest(self, storage_class: str) -> dict:
        """The PVC object to apply, with `storage_class` resolved."""
        return {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": self.name, "namespace": self.namespace},
            "spec": {
                "accessModes": [self.access_mode],
                "storageClassName": storage_class,
                "resources": {"requests": {"storage": self.size}},
            },
        }


# type -> the options class it expects.
OPTIONS_FOR_TYPE = {
    "longhorn": LonghornOptions,
}

# Named so the `options` annotation stays stable: a second implementation
# widens this to a Union and nothing else in the file moves.
StorageOptions = LonghornOptions


class Storage(CapabilitySpec):
    """
    Declarative definition of block storage on an existing cluster.

    `type` and `kubeconfig_path` are required; everything else has a
    working default. `options` defaults to the class matching `type`, so
    most callers never set it.
    """

    # Which implementation provides the storage. Required and explicit —
    # defaulting it would hide the choice this class exists to expose.
    type: str
    kubeconfig_path: str

    # -- meaningful for every implementation --------------------------
    # Copies of each volume. Needs at least this many schedulable nodes,
    # or volumes sit Degraded forever.
    replica_count: int = 3
    # Make this the cluster's default StorageClass, so a PVC naming no
    # storageClassName still binds.
    default_storage_class: bool = True
    # None means the chosen implementation's default namespace.
    namespace: Optional[str] = None
    # Pin for reproducibility: unpinned, two runs a month apart can
    # install different versions from the same spec.
    chart_version: Optional[str] = None
    # Merged into the rendered Helm values, for anything unmodelled.
    extra_values: Dict[str, Any] = Field(default_factory=dict)

    # -- implementation-specific --------------------------------------
    options: Optional[StorageOptions] = None

    # -- the capability contract (see multistack/capability.py) -------
    CAPABILITY: ClassVar[str] = "storage"
    # Block storage installs into a running cluster. Any Kubernetes will
    # do — RKE2, k3s, kubeadm, managed — so this depends on the
    # capability, not on one implementation of it.
    REQUIRES: ClassVar[tuple] = ("cluster",)
    # Fields a Stack can fill, and what this publishes for later layers.
    # storage_class_name is only meaningful once create() has run, which is
    # why Stack.record() is called after the backend, not before.
    FROM_STACK: ClassVar[dict] = {"kubeconfig_path": "kubeconfig_path"}
    PROVIDES: ClassVar[dict] = {"storage_class": "storage_class_name"}
    SUPPORTED_TYPES: ClassVar[tuple] = SUPPORTED_TYPES
    OPTIONS_FOR_TYPE: ClassVar[dict] = OPTIONS_FOR_TYPE
    DEFAULT_NAMESPACES: ClassVar[dict] = {"longhorn": "longhorn-system"}

    @field_validator("kubeconfig_path")
    @classmethod
    def _kubeconfig_required(cls, value: str) -> str:
        if not value:
            raise ValueError(
                "kubeconfig_path is required — this deploys into an existing "
                "cluster, and the SDK won't fall back to ambient "
                "$KUBECONFIG/~/.kube/config, which can silently target the "
                "wrong cluster."
            )
        return value

    @field_validator("replica_count")
    @classmethod
    def _at_least_one_replica(cls, value: int) -> int:
        if value < 1:
            raise ValueError(f"replica_count must be at least 1, got {value}")
        return value

    def validate(self) -> None:
        """Checks the spec is coherent before anything reaches a cluster.

        Construction and assignment already ran these checks, so this is a
        re-run rather than the first one. `driver_for()` still calls it,
        and it reads correctly at a call site.
        """
        self.validate_capability()

    @property
    def storage_class_name(self) -> str:
        """The StorageClass this spec will produce — what a consuming spec
        (a MinIO tenant, a volume claim) should name.

        Longhorn names the class after the Helm release, so this follows
        `options.release_name` rather than `type`. They are the same by
        default; overriding the release name and returning `type` here
        published a class that did not exist, which surfaces as PVCs stuck
        Pending in whatever consumed it.
        """
        release_name = getattr(self.options, "release_name", None)
        return release_name or self.type
