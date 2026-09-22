"""
MinIO tenant declarative resource model.

Deploys S3-compatible object storage into an existing cluster via the
MinIO Operator Helm charts. Like `LonghornStorage` and `Inference` and
unlike `RKE2Cluster`, this deploys *into* a running cluster, so the spec
carries `kubeconfig_path`.

Object storage is the right tier for large immutable blobs — model
weights, datasets, backups. `endpoint()` is what hands those to a
consumer: an `Inference` pointed at `s3://<bucket>/<model>` reads its
weights straight out of a tenant defined here, so nothing has to reach
the internet.
"""
from __future__ import annotations

import re
import secrets
import string
from typing import ClassVar, Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Helm repo hosting both the operator and tenant charts. The chart
# references below are resolved against the local alias, so the two have
# to stay in sync: `minio/operator` only resolves once a repo named
# `minio` has been added.
DEFAULT_HELM_REPO_NAME = "minio"
DEFAULT_HELM_REPO_URL = "https://operator.min.io/"

# MinIO erasure coding needs at least 4 drives across the tenant, which is
# what makes a "distributed" tenant able to lose one and keep serving.
MIN_DISTRIBUTED_DRIVES = 4

# Kubernetes quantity suffixes, for comparing volume sizes without
# pulling in a dependency just to parse "10Gi".
_QUANTITY_UNITS = {
    "": 1,
    "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "P": 10**15,
    "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50,
}


def parse_quantity(qty: str) -> int:
    """Parse a Kubernetes resource quantity (e.g. '20Gi', '500M') into bytes."""
    match = re.match(r"^([0-9.]+)\s*([A-Za-z]*)$", qty.strip())
    if not match:
        raise ValueError(f"Cannot parse Kubernetes quantity: {qty!r}")
    value, unit = match.groups()
    if unit not in _QUANTITY_UNITS:
        raise ValueError(f"Unknown unit in Kubernetes quantity: {qty!r}")
    return int(float(value) * _QUANTITY_UNITS[unit])


def _generate_credential(length: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


class MinIOTenant(BaseModel):
    """
    Declarative definition of one MinIO tenant.

    `kubeconfig_path` and `name` are required; everything else has a
    default that works on a cluster with a default StorageClass.
    """
    model_config = ConfigDict(
        # A misspelled field name is a typo, not a value to keep.
        extra="forbid",
        # Re-checks on assignment, so mutating a spec into an invalid
        # state fails where it happens rather than at deploy time.
        validate_assignment=True,
        arbitrary_types_allowed=True,
    )


    kubeconfig_path: str
    name: str

    namespace: str = "minio"
    mode: str = "distributed"

    # Tenant topology. `servers` x `volumes_per_server` is the drive count,
    # and erasure coding needs at least MIN_DISTRIBUTED_DRIVES of them.
    servers: int = 4
    volumes_per_server: int = 2
    volume_size: str = "10Gi"
    # None means "use the cluster default StorageClass". On a fresh RKE2
    # cluster there isn't one — install Longhorn first, or name a class.
    storage_class: Optional[str] = None

    # Left as None these are generated, so a tenant is never created with
    # a guessable credential that someone forgot to override. The
    # generated values come back on the result from `create()`.
    root_user: Optional[str] = None
    root_password: Optional[str] = None

    # The operator is cluster-scoped: one per cluster, many tenants.
    operator_release_name: str = "minio-operator"
    operator_namespace: str = "minio-operator"
    operator_chart: str = "operator"
    tenant_chart: str = "tenant"
    helm_repo_name: str = DEFAULT_HELM_REPO_NAME
    helm_repo_url: str = DEFAULT_HELM_REPO_URL

    # With auto-cert the tenant serves HTTPS signed by the operator's own
    # CA, which no client trusts — so a consumer needs to skip
    # verification (see Inference.s3_insecure_tls). It also decides
    # whether `endpoint()` is https or http.
    request_auto_cert: bool = True

    image: Optional[str] = None
    # Merged into the rendered Helm values, for anything this spec
    # doesn't model.
    extra_values: Dict[str, Any] = Field(default_factory=dict)

    # matchLabels selectors (e.g. [{"app": "vllm"}]) allowed to reach the
    # S3 API on this tenant. Empty => the backend applies no NetworkPolicy,
    # so an existing tenant is untouched until a caller opts in (ADR-006:
    # features attach by config, not code). This is applied standalone by
    # MinIOBackend, not through the tenant chart -- the chart is a
    # third-party one (minio/tenant) and isn't ours to add templates to.
    network_policy_allowed_ingress: List[Dict[str, str]] = Field(default_factory=list)

    SUPPORTED_MODES: ClassVar[tuple] = ("standalone", "distributed")

    # A tenant's PVCs bind against a StorageClass block storage produced,
    # so it depends on that capability as well as on a cluster. Without a
    # class the claims are accepted and sit Pending, and the timeout
    # surfaces as MinIO's problem rather than storage's.
    #
    # Verified by MinIOBackend.check_prerequisites rather than by
    # kube.require_storage_class: the backend's version distinguishes
    # "names a class that does not exist" (fatal) from "names none and
    # there is no default" (a warning, since something else may still
    # provision), which the generic check cannot know.
    REQUIRES: ClassVar[tuple] = ("cluster", "storage")
    FROM_STACK: ClassVar[dict] = {
        "kubeconfig_path": "kubeconfig_path",
        "storage_class": "storage_class",
    }
    PROVIDES: ClassVar[dict] = {"s3_endpoint_url": "endpoint"}

    @model_validator(mode="after")
    def _validate_on_construction(self):
        """Runs validate() at construction, and again on any assignment.

        The point of the model: a spec that exists is a spec that passed.
        validate() stays public because backends call it and it reads well
        at a call site, but it can no longer be the first time anything is
        checked.
        """
        self.validate()
        return self

    def validate(self) -> None:
        """Checks the spec is coherent before anything reaches a cluster.
        Raises `ValueError` on the first problem found."""
        if not self.kubeconfig_path:
            raise ValueError(
                "kubeconfig_path is required — this deploys into an existing "
                "cluster, and the SDK won't fall back to ambient "
                "$KUBECONFIG/~/.kube/config, which can silently target the "
                "wrong cluster."
            )
        if not self.name or not self.namespace:
            raise ValueError("name and namespace are required")

        if self.mode not in self.SUPPORTED_MODES:
            raise ValueError(
                f"Invalid mode '{self.mode}'. Expected one of {self.SUPPORTED_MODES}."
            )
        if self.servers < 1 or self.volumes_per_server < 1:
            raise ValueError(
                f"servers={self.servers} and volumes_per_server="
                f"{self.volumes_per_server} must both be at least 1"
            )
        if self.mode == "standalone" and self.servers != 1:
            raise ValueError(
                f"mode='standalone' means a single server, got servers="
                f"{self.servers}. Use mode='distributed' for more."
            )
        if self.mode == "distributed" and self.drive_count < MIN_DISTRIBUTED_DRIVES:
            raise ValueError(
                f"A distributed tenant needs at least {MIN_DISTRIBUTED_DRIVES} "
                f"drives for erasure coding, but servers={self.servers} x "
                f"volumes_per_server={self.volumes_per_server} is "
                f"{self.drive_count}. Raise either, or use mode='standalone'."
            )

        # Fail here rather than letting Helm reject it after the operator
        # is already installed.
        parse_quantity(self.volume_size)

        if bool(self.root_user) != bool(self.root_password):
            raise ValueError(
                "root_user and root_password must be set together, or both "
                "left unset to be generated."
            )

    @property
    def drive_count(self) -> int:
        """Total drives across the tenant, which is what erasure coding
        is configured from."""
        return self.servers * self.volumes_per_server

    @property
    def config_secret_name(self) -> str:
        """The Secret the operator reads the root credentials from."""
        return f"{self.name}-env-configuration"

    @property
    def label_selector(self) -> str:
        """Selects everything belonging to this tenant — pods and the
        PVCs the operator creates for it."""
        return f"v1.min.io/tenant={self.name}"

    def ensure_credentials(self) -> None:
        """Fill in any credential that wasn't supplied.

        Generated rather than defaulted to something like `admin`, so a
        tenant is never quietly created with a credential that everyone
        who has read the docs already knows.
        """
        self.set_credentials(
            self.root_user or _generate_credential(16),
            self.root_password or _generate_credential(24),
        )

    def set_credentials(self, user: str, password: str) -> None:
        """Sets both credentials as one change.

        They have to move together: assigning them one at a time
        re-validates after the first, in the state where root_user is set
        and root_password is not — exactly what the set-together rule
        forbids. So this is the only supported way to change them, and the
        rule stays enforceable.
        """
        object.__setattr__(self, "root_user", user)
        object.__setattr__(self, "root_password", password)
        self.validate()

    def endpoint(self) -> str:
        """In-cluster S3 URL for this tenant.

        This is the value a consumer wants — hand it to
        `Inference.s3_endpoint_url` to serve model weights from here
        rather than from the internet. The operator names the S3 service
        `minio` regardless of tenant name; the console and headless
        services carry the tenant name instead.
        """
        scheme = "https" if self.request_auto_cert else "http"
        return f"{scheme}://minio.{self.namespace}.svc.cluster.local"

    def helm_values(self) -> Dict[str, Any]:
        """The tenant chart's values, as a plain dict.

        Written to a file rather than passed as `--set`: the root
        credentials would otherwise be visible in the process list of
        every user on the machine.
        """
        pool: Dict[str, Any] = {
            "name": "pool-0",
            "servers": self.servers,
            "volumesPerServer": self.volumes_per_server,
            "size": self.volume_size,
        }
        if self.storage_class:
            pool["storageClassName"] = self.storage_class

        tenant: Dict[str, Any] = {
            "name": self.name,
            "configSecret": {
                "name": self.config_secret_name,
                "accessKey": self.root_user,
                "secretKey": self.root_password,
            },
            "pools": [pool],
            "certificate": {"requestAutoCert": self.request_auto_cert},
        }
        if self.image:
            tenant["image"] = self.image

        values: Dict[str, Any] = {"tenant": tenant}
        values.update(self.extra_values)
        return values
