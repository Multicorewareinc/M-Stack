"""The queue capability: the async event backbone every counting-based
service in this platform reads and writes (ADR-007/008/030).

    Queue(kubeconfig_path=kc, storage_class="longhorn")

`nats` is the only implementation: JetStream, through the official Helm
chart. The mechanics -- Helm install, the manual-admin client, idempotent
stream creation -- live in `multistack.nats.deployment` and are wrapped
here rather than reimplemented; see that package's own docstrings for
the kubectl/Helm detail. What this module adds is the
`CapabilitySpec`/`CapabilityBackend` contract every other capability in
the SDK already has: `type`/`options`, `REQUIRES`, `FROM_STACK`,
`PROVIDES`, and state tracking -- none of which `NatsDeployment` carries,
because it predates this capability and is a plain `pydantic.BaseModel`.

Named for what a dependent asks for, not who provides it -- the same
reason `Cache` is named for `cache_url` rather than `Valkey`.
`event_backbone_url` is a field `Gateway`, `Policy`, `Enricher` and
`Billing` already read (some through `FROM_STACK`, some as an explicit
argument where an empty value is a stated `allow_no_backbone=True`
decision rather than an accident) — this capability is what fills it in.

Persistent, like `Observability` and `MinIOTenant`: `REQUIRES = ("cluster",
"storage")`. JetStream's file store needs a StorageClass, or every
retained message is gone the moment the pod reschedules.

`DEFAULT_NAMESPACES` is `platform`, not this implementation's own
preferred namespace -- see the constant's own comment below.
"""
from typing import Any, ClassVar, Dict, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..capability import CapabilitySpec

SUPPORTED_TYPES: Tuple[str, ...] = ("nats",)

# This capability's own NATS runs in `platform`, which every consumer's
# `event_backbone_url` default already assumes
# (nats://nats.platform.svc.cluster.local:4222) -- it replaced what used
# to be a hand-deployed NATS at that same address. Not
# `multistack.nats.deployment.NatsDeployment`'s own default (`nats`) --
# deploying under that default instead would stand up a second,
# differently-addressed NATS nothing points at.
DEFAULT_NAMESPACE = "platform"


class NatsQueueOptions(BaseModel):
    """Chart/release settings for the JetStream implementation.

    Deliberately not a field-for-field mirror of every
    `NatsDeployment` knob -- these are the ones worth tuning per
    deployment; anything else travels through `extra_values`, the same
    escape hatch `ValkeyOptions.values` and `Observability.extra_values`
    already use.
    """

    model_config = ConfigDict(extra="forbid")

    release_name: str = "nats"
    # Decoupled from release_name, same as `Storage`'s `LonghornOptions.chart`
    # -- a local chart path or a mirror can be substituted without renaming
    # the release itself.
    chart: str = "nats"
    chart_version: Optional[str] = None

    # JetStream needs a quorum for the streams it keeps -- see the
    # validator below. 3 is the HA topology this implementation targets;
    # the hand-deployed NATS it replaced ran a single, unreplicated pod
    # instead -- losing that node lost the backbone.
    replicas: int = 3

    pvc_size: str = "20Gi"
    file_store_max_size: str = "15Gi"
    memory_store_max_size: str = "2Gi"
    image_tag: str = "2.14.6-alpine"

    # Merged into the rendered Helm values, for anything unmodelled --
    # the same escape hatch every other capability's options model has.
    extra_values: Dict[str, Any] = Field(default_factory=dict)

    def validate(self) -> None:
        if self.replicas < 2:
            raise ValueError(
                f"replicas={self.replicas} is below the JetStream quorum "
                "floor of 2 -- see multistack.nats.deployment.NatsDeployment, "
                "which this wraps and validates the same way."
            )


class Queue(CapabilitySpec):
    """The async event backbone on an existing cluster."""

    CAPABILITY: ClassVar[str] = "queue"
    SUPPORTED_TYPES: ClassVar[Tuple[str, ...]] = SUPPORTED_TYPES
    OPTIONS_FOR_TYPE: ClassVar[Dict[str, type]] = {"nats": NatsQueueOptions}
    DEFAULT_NAMESPACES: ClassVar[Dict[str, str]] = {"nats": DEFAULT_NAMESPACE}
    REQUIRES: ClassVar[Tuple[str, ...]] = ("cluster", "storage")

    FROM_STACK: ClassVar[Dict[str, str]] = {
        "kubeconfig_path": "kubeconfig_path",
        "storage_class": "storage_class",
    }
    # `event_backbone_url` is the key Gateway/Policy/Enricher/Billing
    # already read. Recording a Queue is what fills it in.
    PROVIDES: ClassVar[Dict[str, str]] = {"event_backbone_url": "endpoint"}

    type: str = "nats"
    kubeconfig_path: str
    namespace: Optional[str] = None
    options: Optional[NatsQueueOptions] = None

    # None means "use the cluster's default StorageClass" -- the same
    # convention Observability and MinIOTenant use, and the field
    # CapabilityBackend's generic `verify_requirements` reads to check
    # the "storage" dependency (see multistack/capability.py).
    storage_class: Optional[str] = None

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

    def validate(self) -> None:
        """Checks the spec is coherent before anything reaches a cluster.

        Construction and assignment already ran these checks, so this is
        a re-run rather than the first one, same as `Observability.validate`.
        """
        self.validate_capability()

    def _release_name(self) -> str:
        return self.options.release_name if self.options else "nats"

    @property
    def endpoint(self) -> str:
        """In-cluster NATS client URL.

        Same DNS-name convention `multistack.nats.deployment.NatsAdminClient
        .server_url` computes, since the driver delegates the actual
        deployment to that package -- this is not a second calculation
        that could drift from it, just the same string built from this
        spec's own fields instead of a `NatsDeployment`.
        """
        return (
            f"nats://{self._release_name()}.{self.resolved_namespace}"
            ".svc.cluster.local:4222"
        )
