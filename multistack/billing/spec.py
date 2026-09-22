"""The billing capability: usage metering and Stripe subscriptions.

    Billing(kubeconfig_path=kc, existing_secret="billing-secrets")

A FastAPI service with its own Postgres schema and migration Job, the
same shape as `ControlPlane`. Two independent surfaces in one service
(ADR-030/031/032):

  consumer   drains the enricher's derived `gateway.events.enriched`
             stream into `usage`/`outbox` rows -- inert with no
             `event_backbone_url`, same discipline as every other
             consumer here.
  reporter   polls `outbox` and calls Stripe's Meter Events API, and
             serves POST /internal/v1/subscriptions (called by
             admin-control-plane on org/plan changes) and POST
             /webhooks/stripe. Both genuinely optional: an unset
             `stripe_secret_key`/`stripe_webhook_secret` leaves the
             reporter and the webhook route fully inert rather than
             broken -- "usage metering only, no Stripe integration yet"
             is a valid, supported configuration.

Credentials never appear here. The chart reads DATABASE_URL,
SERVICE_API_KEY and the two optional Stripe secrets from a Kubernetes
Secret, and this spec carries only that Secret's *name* -- see
`existing_secret`, the same design as `ControlPlane.existing_secret`.
`stripe` is the only implementation: `billing` names what a dependent
asks for, `type="stripe"` names who actually processes payments, the
same relationship `Cache(type="valkey")` and `Database(type="cnpg")`
have.

`billing` has no SDK consumer yet: nothing else's `REQUIRES` names it,
and `admin-control-plane`'s own `BILLING_INTERNAL_URL` has no chart hook
to receive it from this spec (a gap in that chart, not in this one) --
see ARCHITECTURE-GAPS.md. `PROVIDES` is still declared, on the same
reasoning `Tokenizer.PROVIDES` has: a real output worth publishing even
before anything reads it automatically.
"""
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..capability import CapabilitySpec

SUPPORTED_TYPES: Tuple[str, ...] = ("stripe",)

DEFAULT_NAMESPACE = "billing"
DEFAULT_PORT = 8000

# Every key the chart's Secret must carry. Named here so
# `check_prerequisites` can say which one is missing rather than letting
# the pod crash-loop or (worse) start serving with a credential absent.
# Not STRIPE_SECRET_KEY/STRIPE_WEBHOOK_SECRET: both are genuinely
# optional (see the module docstring), so their absence is not a
# prerequisite failure.
REQUIRED_SECRET_KEYS: Tuple[str, ...] = ("DATABASE_URL", "SERVICE_API_KEY")


class StripeOptions(BaseModel):
    """Deployment settings for the stripe-backed implementation."""

    model_config = ConfigDict(extra="forbid")

    release_name: str = "billing"
    chart: str = "api/microservices/billing/chart"
    chart_version: Optional[str] = None
    image_tag: Optional[str] = None

    durable_name: str = "billing-metering"

    stripe_meter_event_name: str = "tokens_used"
    # The Meter Event payload key `principal` is sent under. Documented
    # limitation (AD-06): `principal` is an internal org_id, not yet a
    # real Stripe Customer ID, until the org->customer mapping lands.
    stripe_meter_customer_field: str = "stripe_customer_id"

    reporter_poll_interval_seconds: int = Field(default=5, gt=0)
    reporter_batch_size: int = Field(default=50, ge=1)
    reporter_max_attempts: int = Field(default=5, ge=1)
    reporter_backoff_base_seconds: int = Field(default=30, gt=0)
    reporter_backoff_max_seconds: int = Field(default=3600, gt=0)

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
        if self.reporter_backoff_base_seconds > self.reporter_backoff_max_seconds:
            raise ValueError(
                "reporter_backoff_base_seconds "
                f"({self.reporter_backoff_base_seconds}) is greater than "
                f"reporter_backoff_max_seconds "
                f"({self.reporter_backoff_max_seconds}) -- the first retry "
                "would already be capped, which is not what either value "
                "is for."
            )


OPTIONS_FOR_TYPE = {"stripe": StripeOptions}
BillingOptions = StripeOptions


class Billing(CapabilitySpec):
    """Usage metering and Stripe subscriptions on an existing cluster."""

    CAPABILITY: ClassVar[str] = "billing"
    SUPPORTED_TYPES: ClassVar[Tuple[str, ...]] = SUPPORTED_TYPES
    OPTIONS_FOR_TYPE: ClassVar[Dict[str, type]] = OPTIONS_FOR_TYPE
    DEFAULT_NAMESPACES: ClassVar[Dict[str, str]] = {"stripe": DEFAULT_NAMESPACE}

    # A database it owns exclusively (database-per-service) and, like
    # `ControlPlane`, no cache -- billing has no VALKEY_URL/REDIS_URL in
    # its settings at all.
    REQUIRES: ClassVar[Tuple[str, ...]] = ("cluster", "database")
    FROM_STACK: ClassVar[Dict[str, str]] = {
        "kubeconfig_path": "kubeconfig_path",
    }
    PROVIDES: ClassVar[Dict[str, str]] = {"billing_endpoint": "endpoint"}

    type: str = "stripe"
    kubeconfig_path: str
    namespace: Optional[str] = None
    options: Optional[BillingOptions] = None

    # The Secret holding every key in REQUIRED_SECRET_KEYS, plus
    # optionally STRIPE_SECRET_KEY/STRIPE_WEBHOOK_SECRET. Referenced by
    # name, never created here: a spec is a file people commit, and a
    # password in one is a password in git.
    existing_secret: str

    replicas: int = Field(default=1, ge=1)
    service_port: int = Field(default=DEFAULT_PORT, gt=0, le=65535)
    log_level: str = "INFO"

    # Counting is asynchronous: this consumes the enricher's derived
    # stream and drains it into usage/outbox rows. Empty means no
    # consumer, so nothing is ever metered. See the validator below.
    event_backbone_url: str = ""
    allow_no_backbone: bool = False

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
                "existing_secret is required: billing reads DATABASE_URL "
                "and SERVICE_API_KEY (and optionally the Stripe secrets) "
                "from a Kubernetes Secret, and this spec carries only its "
                "name. Create the Secret with multistack.kube.apply, which "
                "pipes the manifest to stdin — `kubectl create secret "
                "--from-literal` puts the value in the process arguments, "
                "where any local user can read it."
            )
        return value

    @model_validator(mode="after")
    def _validate_on_construction(self) -> "Billing":
        """Runs `validate()` at construction, and again on assignment.

        `CapabilitySpec`'s own validator runs `validate_capability()`
        only, so without this the empty-backbone check below would not
        fire until a backend dispatched. Same shape as `Policy`'s and
        `Enricher`'s.
        """
        self.validate()
        return self

    def validate(self) -> None:
        self.validate_capability()
        if not self.event_backbone_url and not self.allow_no_backbone:
            raise ValueError(
                "event_backbone_url is empty, so nothing would be "
                "metered.\n\n"
                "Without a backbone this service's consumer never runs: "
                "usage events are never recorded, the outbox never fills, "
                "and the Stripe reporter (if configured) has nothing to "
                "drain — a billing service that is deployed, healthy, and "
                "metering nothing.\n\n"
                "Set event_backbone_url, or allow_no_backbone=True to "
                "deploy without metering deliberately (useful for standing "
                "up the DB/API surface before NATS exists)."
            )

    @property
    def release_name(self) -> str:
        return self.options.release_name if self.options else "billing"

    @property
    def endpoint(self) -> str:
        """In-cluster URL, with no credential in it."""
        return (
            f"http://{self.release_name}.{self.resolved_namespace}"
            f".svc.cluster.local:{self.service_port}"
        )
