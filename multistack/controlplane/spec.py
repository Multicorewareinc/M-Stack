"""The control plane capability: the platform's own API services.

    ControlPlane(type="admin", kubeconfig_path=kc,
                 existing_secret="admin-cp-secrets")

Two implementations of one capability, because they are the same shape
deployed twice: the admin control plane owns plans, permissions and
platform-wide administration; the organization control plane owns an
organization's own users, keys and quotas. Each is a FastAPI service with
a Postgres schema, a migration Job, and a peer it calls over the cluster
network.

Credentials never appear here. Both charts read DATABASE_URL, VALKEY_URL,
JWT_SECRET and their API keys from a Kubernetes Secret, and this spec
carries only that Secret's *name* -- see `existing_secret`. The Stack's
published `database_url` and `cache_url` are deliberately credential-free
and so cannot serve, which is why they are in `REQUIRES` (the layers must
exist first) but not in `FROM_STACK`.
"""
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..capability import CapabilitySpec

SUPPORTED_TYPES: Tuple[str, ...] = ("admin", "organization")

DEFAULT_PORT = 8000

# Every key the chart's Secret must carry, by type. Named here so
# `check_prerequisites` can say which one is missing rather than letting
# the pod crash-loop on a KeyError, and so the message is one list rather
# than one discovery per deploy.
REQUIRED_SECRET_KEYS: Dict[str, Tuple[str, ...]] = {
    "admin": (
        "DATABASE_URL", "VALKEY_URL", "JWT_SECRET",
        "ADMIN_API_KEY", "SERVICE_API_KEY",
    ),
    "organization": (
        "DATABASE_URL", "VALKEY_URL", "JWT_SECRET", "SERVICE_API_KEY",
        # The bearer the model-gateway sends to verify a per-org API key
        # (ADR-026). Distinct from SERVICE_API_KEY on purpose
        # (least-privilege) and must equal the gateway release's own
        # ORG_VERIFY_API_KEY exactly, or every /v1 request the gateway
        # proxies fails closed with a 503.
        "MG_SERVICE_API_KEY",
    ),
}


class _ControlPlaneOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    release_name: str
    chart: str
    chart_version: Optional[str] = None
    image_tag: Optional[str] = None

    # The migration Job runs before the Deployment is ready. Short enough
    # that a wedged migration fails the install rather than hanging it.
    migration_deadline_seconds: int = Field(default=120, gt=0)
    migration_backoff_limit: int = Field(default=2, ge=0)

    @field_validator("release_name", "chart")
    @classmethod
    def _not_empty(cls, value: str, info) -> str:
        if not value:
            raise ValueError(f"{info.field_name} must not be empty")
        return value

    def validate(self) -> None:
        self.model_validate(self.model_dump())


class AdminControlPlaneOptions(_ControlPlaneOptions):
    release_name: str = "admin-control-plane"
    chart: str = "api/microservices/admin-control-plane/chart"

    # Seeding is idempotent per row but not concurrency-safe across pods:
    # two replicas racing on first install both insert the same plan and
    # one dies on the unique index. Leave on for a first install, off for
    # a redeploy of a populated database.
    seed_plans: bool = True
    seed_permissions: bool = True


class OrganizationControlPlaneOptions(_ControlPlaneOptions):
    release_name: str = "organization-control-plane"
    chart: str = "api/microservices/organization-control-plane/chart"


OPTIONS_FOR_TYPE = {
    "admin": AdminControlPlaneOptions,
    "organization": OrganizationControlPlaneOptions,
}
ControlPlaneOptions = _ControlPlaneOptions


class ControlPlane(CapabilitySpec):
    """One of the platform's control plane services."""

    CAPABILITY: ClassVar[str] = "controlplane"
    SUPPORTED_TYPES: ClassVar[Tuple[str, ...]] = SUPPORTED_TYPES
    OPTIONS_FOR_TYPE: ClassVar[Dict[str, type]] = OPTIONS_FOR_TYPE
    DEFAULT_NAMESPACES: ClassVar[Dict[str, str]] = {
        "admin": "control-plane", "organization": "control-plane",
    }

    # A control plane is useless without its database, and its login path
    # reads the lockout counters from the cache before it verifies a
    # password -- with no cache every sign-in is a 500, which is how this
    # was found the first time.
    REQUIRES: ClassVar[Tuple[str, ...]] = ("cluster", "database", "cache")
    FROM_STACK: ClassVar[Dict[str, str]] = {
        "kubeconfig_path": "kubeconfig_path",
    }
    PROVIDES: ClassVar[Dict[str, str]] = {"controlplane_endpoint": "endpoint"}

    type: str = "admin"
    kubeconfig_path: str
    namespace: Optional[str] = None
    options: Optional[ControlPlaneOptions] = None

    # The Secret holding every key in REQUIRED_SECRET_KEYS for this type.
    # Referenced by name, never created here: a spec is a file people
    # commit, and a password in one is a password in git.
    existing_secret: str

    replicas: int = Field(default=2, ge=1)
    service_port: int = Field(default=DEFAULT_PORT, gt=0, le=65535)
    log_level: str = "INFO"

    # Where this service reaches its peer. Defaults are the in-cluster
    # Service names both charts already assume.
    peer_url: Optional[str] = None
    peer_timeout_ms: int = Field(default=3000, gt=0)

    # Where this reaches billing. Both types have a use for it, each its
    # own call site: admin-cp's best-effort org/plan-change notify (POST
    # /internal/v1/subscriptions, ADR-032), org-cp's usage read-through
    # proxy (GET /v1/usage/*, add-org-cp-usage-proxy). Unset is a
    # supported deployment stage (billing not deployed yet), not an
    # error, so this has no required-field guard the way peer_url
    # effectively does: unset here just means each service's own
    # settings.py default (http://billing:8000) applies, which resolves
    # nowhere on a real cluster and silently breaks that call site.
    billing_internal_url: Optional[str] = None

    # Scheduling. There is no in-cluster registry, so a service image
    # exists only on the node it was imported to, and a pod scheduled
    # anywhere else stays ImagePullBackOff with nothing in the release to
    # explain it. The chart's own affinity already keeps pods off the
    # control plane; this is how a caller says *which* worker.
    node_selector: Optional[Dict[str, str]] = None
    tolerations: Optional[List[Dict[str, Any]]] = None

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

    @field_validator("existing_secret")
    @classmethod
    def _secret_required(cls, value: str) -> str:
        if not value:
            raise ValueError(
                "existing_secret is required: the control plane reads "
                "DATABASE_URL, VALKEY_URL and JWT_SECRET from a Kubernetes "
                "Secret, and this spec carries only its name. Create the "
                "Secret with multistack.kube.apply, which pipes the manifest "
                "to stdin — `kubectl create secret --from-literal` puts the "
                "value in the process arguments, where any local user can "
                "read it."
            )
        return value

    def validate(self) -> None:
        self.validate_capability()

    @property
    def release_name(self) -> str:
        return self.options.release_name if self.options else f"{self.type}-control-plane"

    @property
    def required_secret_keys(self) -> Tuple[str, ...]:
        return REQUIRED_SECRET_KEYS[self.type]

    @property
    def peer_type(self) -> str:
        return "organization" if self.type == "admin" else "admin"

    @property
    def resolved_peer_url(self) -> str:
        """Where this service calls its counterpart.

        Defaults to the peer's own Service name, which is what both charts
        assume and what makes a two-service install work with no wiring.
        """
        if self.peer_url:
            return self.peer_url
        return (
            f"http://{self.peer_type}-control-plane"
            f".{self.resolved_namespace}.svc.cluster.local:{DEFAULT_PORT}"
        )

    @property
    def endpoint(self) -> str:
        """In-cluster URL, with no credential in it."""
        return (
            f"http://{self.release_name}.{self.resolved_namespace}"
            f".svc.cluster.local:{self.service_port}"
        )
