"""
SeaweedFS cluster declarative resource model.

Deploys S3-compatible object storage into an existing cluster via the
official SeaweedFS Helm chart. Unlike MinIO's split operator/tenant
charts, there is no separate cluster-scoped operator: one Helm release
runs master + volume + filer + s3 together, so this spec is one release
per cluster instance rather than one shared operator plus many tenants.

Object storage is the right tier for large immutable blobs — model
weights, datasets, backups. `endpoint()` is what hands those to a
consumer: an `Inference` pointed at `s3://<bucket>/<model>` reads its
weights straight out of a cluster defined here, so nothing has to reach
the internet.

Chart schema verified against the chart source (github.com/seaweedfs/
seaweedfs, path k8s/charts/seaweedfs, chart version 4.47.1) on
2026-09-17, by reading `values.yaml` and the `master`/`filer`/`volume`/
`s3` templates plus `templates/shared/_helpers.tpl` directly from
GitHub — `helm show values` was not available (no `helm` on PATH in this
environment, and `helm repo add`/`helm show values` both failed for lack
of the binary). Every field below that claims to match a chart value
(`master.replicas`, `s3.port`, `s3.existingConfigSecret`, the
`{fullname}-{component}` service naming, ...) was read from that source,
not inferred — see the docstrings on `chart_fullname()` and `endpoint()`
for the exact templates each one mirrors.
"""
from __future__ import annotations

import json
import re
from typing import ClassVar, Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ._quantity import _generate_credential, parse_quantity

# One release deploys master+volume+filer+s3 together, so — unlike
# MinIO's operator/tenant split — there is only ever one chart to
# resolve here.
DEFAULT_HELM_REPO_NAME = "seaweedfs"
DEFAULT_HELM_REPO_URL = "https://seaweedfs.github.io/seaweedfs/helm"
DEFAULT_CHART_NAME = "seaweedfs"

# The chart's own default S3 gateway port (`s3.port` in values.yaml).
# The gateway itself defaults to disabled in the chart (`s3.enabled:
# false`) since not every SeaweedFS deployment wants one; this spec
# always turns it on, since S3 access is the point of deploying it here.
DEFAULT_S3_PORT = 8333


def chart_fullname(release_name: str, chart_name: str = DEFAULT_CHART_NAME) -> str:
    """Mirrors the chart's own `seaweedfs.fullname` helper
    (`templates/shared/_helpers.tpl`): the release name as-is when it
    already contains the chart name, else `<release>-<chart>`.

        {{- define "seaweedfs.fullname" -}}
        {{- if .Values.fullnameOverride -}} ...
        {{- else -}}
        {{- $name := default .Chart.Name .Values.nameOverride -}}
        {{- if contains $name .Release.Name -}}
        {{- .Release.Name -}}
        {{- else -}}
        {{- printf "%s-%s" .Release.Name $name -}}
        {{- end -}} ...

    Reproduced here (rather than asked of a running release) so
    `endpoint()` can be computed before anything is installed. This spec
    never sets `fullnameOverride`/`nameOverride`, so those branches are
    the whole rule that applies.
    """
    if chart_name in release_name:
        return release_name
    return f"{release_name}-{chart_name}"


class SeaweedFSCluster(BaseModel):
    """
    Declarative definition of one SeaweedFS cluster: master, volume,
    filer and the S3 gateway, deployed as a single Helm release.

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

    namespace: str = "seaweedfs"

    # Component replica counts. SeaweedFS has no MinIO-style
    # erasure-coding drive-count rule — durability comes from
    # `replication` below, not from how many volume replicas exist, so
    # there is no equivalent of MinIOTenant's MIN_DISTRIBUTED_DRIVES
    # check here.
    master_replicas: int = 1
    filer_replicas: int = 1
    volume_replicas: int = 1

    volume_size: str = "10Gi"
    # None means "use the cluster default StorageClass". On a fresh RKE2
    # cluster there isn't one — install Longhorn first, or name a class.
    storage_class: Optional[str] = None

    # SeaweedFS's own replication code, "XYZ": X extra copies in other
    # data centers, Y in other racks, Z in other servers in the same
    # rack. Fans out to both `master.defaultReplication` and
    # `filer.defaultReplicaPlacement` in helm_values() — both exist in
    # the chart and writes can go through either path, so both need the
    # same durability setting or a write via the filer/S3 gateway would
    # get a different guarantee than one via the master. "000" is the
    # chart's own default and means no redundancy at all.
    replication: str = "000"

    # Left as None these are generated, so a cluster is never created
    # with a guessable credential that someone forgot to override. The
    # generated values come back on the result from `create()`.
    access_key: Optional[str] = None
    secret_key: Optional[str] = None

    # One Helm release deploys everything (master+volume+filer+s3),
    # unlike MinIO's cluster-scoped operator plus one release per
    # tenant. None means "use `name`" — see `resolved_release_name`.
    release_name: Optional[str] = None
    chart_name: str = DEFAULT_CHART_NAME
    helm_repo_name: str = DEFAULT_HELM_REPO_NAME
    helm_repo_url: str = DEFAULT_HELM_REPO_URL

    # The chart's S3 gateway port (`s3.port`). Overridable for a cluster
    # that changed it via extra_values and needs endpoint() to agree.
    s3_port: int = DEFAULT_S3_PORT

    image: Optional[str] = None
    # Merged into the rendered Helm values, for anything this spec
    # doesn't model.
    extra_values: Dict[str, Any] = Field(default_factory=dict)

    # matchLabels selectors (e.g. [{"app": "vllm"}]) allowed to reach the
    # S3 API on this cluster. Empty => the backend applies no
    # NetworkPolicy, so an existing cluster is untouched until a caller
    # opts in (ADR-006: features attach by config, not code). Applied
    # standalone by SeaweedFSBackend, not through the chart —
    # seaweedfs/seaweedfs is a third-party chart and isn't ours to add
    # templates to.
    network_policy_allowed_ingress: List[Dict[str, str]] = Field(default_factory=list)

    # A cluster's PVCs bind against a StorageClass block storage
    # produced, so it depends on that capability as well as on a
    # cluster. Verified by SeaweedFSBackend.check_prerequisites rather
    # than by kube.require_storage_class, for the same reason MinIO's
    # did: distinguishing "names a class that does not exist" (fatal)
    # from "names none and there is no default" (a warning).
    REQUIRES: ClassVar[tuple] = ("cluster", "storage")
    FROM_STACK: ClassVar[dict] = {
        "kubeconfig_path": "kubeconfig_path",
        "storage_class": "storage_class",
    }
    PROVIDES: ClassVar[dict] = {"s3_endpoint_url": "endpoint"}

    @model_validator(mode="after")
    def _validate_on_construction(self):
        """Runs validate() at construction, and again on any assignment.

        The point of the model: a spec that exists is a spec that
        passed. validate() stays public because backends call it and it
        reads well at a call site, but it can no longer be the first
        time anything is checked.
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

        for field_name, value in (
            ("master_replicas", self.master_replicas),
            ("filer_replicas", self.filer_replicas),
            ("volume_replicas", self.volume_replicas),
        ):
            if value < 1:
                raise ValueError(f"{field_name}={value} must be at least 1")

        if not re.match(r"^[0-9]{3}$", self.replication or ""):
            raise ValueError(
                f"Invalid replication code '{self.replication}'. Expected "
                "three digits XYZ — extra copies in other data centers, "
                "racks, and servers respectively — e.g. '000' (none, the "
                "chart's own default) or '001' (one extra copy on another "
                "server in the same rack)."
            )

        # Fail here rather than letting Helm reject it after the release
        # is already installed.
        parse_quantity(self.volume_size)

        if bool(self.access_key) != bool(self.secret_key):
            raise ValueError(
                "access_key and secret_key must be set together, or both "
                "left unset to be generated."
            )

    @property
    def resolved_release_name(self) -> str:
        """The Helm release name — `name`, unless overridden."""
        return self.release_name or self.name

    @property
    def fullname(self) -> str:
        """What the chart's own `seaweedfs.fullname` helper resolves to
        for this release — see `chart_fullname()`. Every component's
        Service is named `{fullname}-{component}` by the chart's
        `seaweedfs.componentName` helper, which is what `endpoint()` and
        `label_selector` below both depend on."""
        return chart_fullname(self.resolved_release_name, self.chart_name)

    @property
    def s3_service_name(self) -> str:
        """The chart's S3 Service name — `templates/s3/s3-service.yaml`
        names it `{{ include "seaweedfs.componentName" (list . "s3") }}`,
        i.e. `{fullname}-s3`."""
        return f"{self.fullname}-s3"

    @property
    def config_secret_name(self) -> str:
        """The Secret this backend manages itself (applied with `kubectl
        apply`, not rendered by the chart) and points the release at via
        `s3.existingConfigSecret`. Deliberately not the chart's own
        auto-generated `{fullname}-s3-secret` — owning the Secret
        ourselves is what makes the adopt-don't-regenerate credential
        behaviour possible (see SeaweedFSBackend.create)."""
        return f"{self.name}-s3-config"

    @property
    def label_selector(self) -> str:
        """Selects every pod belonging to this release. The chart labels
        every component's pods with both of these
        (`templates/shared/_helpers.tpl` / every template's own
        `labels:` block): `app.kubernetes.io/instance` is the release
        name and `app.kubernetes.io/name` is the chart name (absent a
        `nameOverride`, which this spec never sets)."""
        return (
            f"app.kubernetes.io/instance={self.resolved_release_name},"
            f"app.kubernetes.io/name={self.chart_name}"
        )

    def ensure_credentials(self) -> None:
        """Fill in any credential that wasn't supplied.

        Generated rather than defaulted to something like `admin`, so a
        cluster is never quietly created with a credential that everyone
        who has read the docs already knows.
        """
        self.set_credentials(
            self.access_key or _generate_credential(16),
            self.secret_key or _generate_credential(24),
        )

    def set_credentials(self, access_key: str, secret_key: str) -> None:
        """Sets both credentials as one change.

        They have to move together: assigning them one at a time
        re-validates after the first, in the state where access_key is
        set and secret_key is not — exactly what the set-together rule
        forbids. So this is the only supported way to change them, and
        the rule stays enforceable.
        """
        object.__setattr__(self, "access_key", access_key)
        object.__setattr__(self, "secret_key", secret_key)
        self.validate()

    def s3_config_json(self) -> str:
        """The `seaweedfs_s3_config` value the S3 gateway reads its
        identities from (chart comment on `s3.existingConfigSecret`:
        "should have a secret key called seaweedfs_s3_config with an
        inline json config"). One identity, named after this cluster,
        granted full access — this spec models a single set of
        credentials, the same shape MinIOTenant's root_user/root_password
        had."""
        return json.dumps({
            "identities": [{
                "name": self.name,
                "credentials": [{
                    "accessKey": self.access_key,
                    "secretKey": self.secret_key,
                }],
                "actions": ["Admin", "Read", "Write"],
            }],
        })

    def endpoint(self) -> str:
        """In-cluster S3 URL for this cluster.

        This is the value a consumer wants — hand it to
        `Inference.s3_endpoint_url` to serve model weights from here
        rather than from the internet. Plain HTTP: the chart has no
        equivalent of the MinIO Operator's per-tenant auto-cert, so TLS
        isn't modelled here (see docs/seaweedfs.md's "Known
        limitations").
        """
        return (
            f"http://{self.s3_service_name}.{self.namespace}"
            f".svc.cluster.local:{self.s3_port}"
        )

    def helm_values(self) -> Dict[str, Any]:
        """The chart's values, as a plain dict.

        Credentials never appear here — they live in a Secret this
        backend manages itself (`s3_config_json()`) and are referenced
        by name via `s3.existingConfigSecret`. That is what makes it
        safe to write this dict to a temp file rather than pass it with
        `--set` (see SeaweedFSBackend._write_values): nothing in it is
        sensitive.
        """
        data_dir: Dict[str, Any] = {
            "name": "data1",
            "type": "persistentVolumeClaim",
            "size": self.volume_size,
            "maxVolumes": 0,
        }
        if self.storage_class:
            data_dir["storageClass"] = self.storage_class

        values: Dict[str, Any] = {
            "master": {
                "replicas": self.master_replicas,
                "defaultReplication": self.replication,
            },
            "filer": {
                "replicas": self.filer_replicas,
                "defaultReplicaPlacement": self.replication,
            },
            "volume": {
                "replicas": self.volume_replicas,
                "dataDirs": [data_dir],
            },
            "s3": {
                "enabled": True,
                "port": self.s3_port,
                "existingConfigSecret": self.config_secret_name,
            },
        }
        if self.image:
            repository, _, tag = self.image.partition(":")
            values["image"] = {"repository": repository}
            if tag:
                values["image"]["tag"] = tag

        values.update(self.extra_values)
        return values
