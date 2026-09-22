"""The enricher capability: guarantees a token count on every response event.

    Enricher(kubeconfig_path=kc, event_backbone_url="nats://nats:4222")

Sits between the gateway's raw event stream and everything downstream
that needs a token count off it. Consumes `gateway.events`' `response`
events (ADR-007, unchanged), falls back to the tokenizer when a response
carries no `usage` block, and republishes to a *derived* stream
(`gateway.events.enriched`, ADR-030) so the enriched output's retention
and cursor stay independent of the raw input. `rate-limiter-tpm` and
`billing` both point at that derived stream instead of calling the
tokenizer themselves -- this capability is what makes that true, and
without it deployed and consuming, both go quietly uncounted rather than
failing.

`enricher` is the only implementation, and `type` names it the same way
`Gateway(type="modelgateway")` does: a bespoke first-party service with
no vendor or algorithm name to borrow.

PROVIDES is deliberately absent, like `Accelerator`'s. Nothing downstream
reaches this over HTTP -- TPM and billing consume its output through NATS
stream/subject strings, which are plain configuration values, not a
Service address there is any reason to publish.
"""
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..capability import CapabilitySpec

SUPPORTED_TYPES: Tuple[str, ...] = ("enricher",)

DEFAULT_NAMESPACE = "policy"
DEFAULT_PORT = 8000

# The tokenizer fallback's own arithmetic guard (ADR-030 D5), mirrored
# from the chart's configmap.yaml `fail()` so a misconfiguration is
# caught at spec-construction time rather than at `helm install`. A 5s
# margin, same as the chart: a floor, not a guarantee.
_TOKENIZER_ACK_MARGIN_MS = 5000


class EnricherOptions(BaseModel):
    """Deployment settings for the enricher implementation."""

    model_config = ConfigDict(extra="forbid")

    release_name: str = "enricher"
    chart: str = "api/microservices/enricher/chart"
    chart_version: Optional[str] = None
    image_tag: Optional[str] = None

    # Input: the gateway's own, unchanged stream (ADR-007). This service
    # consumes it and never modifies its contract.
    event_stream_name: str = "GATEWAY_EVENTS"
    event_stream_subject: str = "gateway.events"
    durable_name: str = "enricher"

    # Output: a separate derived stream (ADR-030 D1), so its retention and
    # cursor stay independent of the raw input's. TPM and billing both
    # point here, not at the input pair above.
    enriched_stream_name: str = "GATEWAY_EVENTS_ENRICHED"
    enriched_subject: str = "gateway.events.enriched"

    # Bounded redelivery for the not-ack-on-republish-failure path
    # (ADR-030 D5): unlike the rate limiters, which always ack (a missed
    # count there is just under-enforcement), a dropped republish here is
    # real data loss for billing, so a failing republish is retried up to
    # `max_deliver` times instead of acked away.
    max_deliver: int = Field(default=5, ge=1)
    ack_wait_seconds: int = Field(default=30, gt=0)

    # Fallback for a response event with no `usage` block. Empty means the
    # fallback is inert: such events are republished with source="none"
    # rather than estimated, so they go uncounted rather than guessed at.
    # That is under-enforcement/under-billing, not failure, which is why
    # it is allowed.
    tokenizer_url: str = ""
    tokenizer_timeout_ms: int = Field(default=2000, ge=1)

    def validate(self) -> None:
        if not self.tokenizer_url:
            return
        # Must cover a tokenizer call plus time to republish, or
        # JetStream redelivers the message while the first attempt is
        # still in flight -- burning max_deliver's budget on attempts
        # that were never really failures.
        min_ms = self.tokenizer_timeout_ms + _TOKENIZER_ACK_MARGIN_MS
        if self.ack_wait_seconds * 1000 < min_ms:
            min_seconds = -(-min_ms // 1000)  # ceil
            raise ValueError(
                f"ack_wait_seconds={self.ack_wait_seconds} "
                f"({self.ack_wait_seconds * 1000}ms) is too close to "
                f"tokenizer_timeout_ms={self.tokenizer_timeout_ms}.\n\n"
                "ack_wait_seconds must cover a tokenizer call plus time to "
                "republish, or JetStream redelivers a message while the "
                "first attempt is still in flight, burning max_deliver's "
                "retry budget on attempts that were never really "
                f"failures. Set ack_wait_seconds to at least {min_seconds} "
                "(tokenizer timeout + a 5s republish margin, rounded up)."
            )


class Enricher(CapabilitySpec):
    """Guarantees a usable token count on every gateway response event."""

    CAPABILITY: ClassVar[str] = "enricher"
    SUPPORTED_TYPES: ClassVar[Tuple[str, ...]] = SUPPORTED_TYPES
    OPTIONS_FOR_TYPE: ClassVar[Dict[str, type]] = {"enricher": EnricherOptions}
    DEFAULT_NAMESPACES: ClassVar[Dict[str, str]] = {"enricher": DEFAULT_NAMESPACE}
    REQUIRES: ClassVar[Tuple[str, ...]] = ("cluster",)

    FROM_STACK: ClassVar[Dict[str, str]] = {
        "kubeconfig_path": "kubeconfig_path",
    }
    # No PROVIDES -- see the module docstring.

    type: str = "enricher"
    kubeconfig_path: str
    namespace: Optional[str] = None
    options: Optional[EnricherOptions] = None

    service_port: int = Field(default=DEFAULT_PORT, gt=0, le=65535)

    # Counting is asynchronous: this consumes events off the backbone and
    # republishes to it. Empty means no consumer, so nothing is enriched
    # and TPM/billing both go quietly uncounted. See the validator below.
    event_backbone_url: str = ""
    allow_no_backbone: bool = False

    replicas: int = Field(default=1, ge=1, le=1)

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

    @model_validator(mode="after")
    def _validate_on_construction(self) -> "Enricher":
        """Runs `validate()` at construction, and again on assignment,
        same as `Cache`'s and `IngressGateway`'s -- `CapabilitySpec`'s own
        validator runs `validate_capability()` only."""
        self.validate()
        return self

    def validate(self) -> None:
        self.validate_capability()
        if not self.event_backbone_url and not self.allow_no_backbone:
            raise ValueError(
                "event_backbone_url is empty, so nothing would be "
                "enriched.\n\n"
                "Without a backbone this service consumes nothing and "
                "republishes nothing: rate-limiter-tpm and billing both "
                "get zero enrichment forever, with no error on either "
                "side — an enricher that is deployed, healthy, and doing "
                "nothing.\n\n"
                "Set event_backbone_url, or allow_no_backbone=True to "
                "deploy an inert enricher deliberately (useful for wiring "
                "up the rest of the pipeline before NATS exists)."
            )

    @property
    def release_name(self) -> str:
        return self.options.release_name if self.options else "enricher"

    @property
    def endpoint(self) -> str:
        """In-cluster URL, with no credential in it -- for health checks
        and metrics scraping. Nothing downstream calls it: consumers
        reach this service's output through the enriched NATS
        stream/subject, not through this address."""
        return (
            f"http://{self.release_name}.{self.resolved_namespace}"
            f".svc.cluster.local:{self.service_port}"
        )
