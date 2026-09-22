"""The policy capability: a pre-request check the gateway calls.

    Policy(type="rpm", kubeconfig_path=kc, cache_url=..., limits=...)

A policy answers `POST /check` with allow or deny, and attaches to the
gateway by configuration alone — the gateway image does not change when
one is added (ADR-006). `rpm` counts requests and `tpm` counts tokens:
the same contract over a different counter, which is why they share this
spec rather than each having their own. Quota would be a third.

What this buys over passing chart values as a dict: the limits document
is checked here, at the call site, instead of at deploy time. That
matters more than it sounds, because the service parses RATE_LIMITS with
a fallback — a document it cannot read becomes `{}`, every scope goes
unlimited, and nothing raises. A limiter that is Ready and enforcing
nothing looks exactly like one that is working.
"""
from typing import Any, ClassVar, Dict, List, Optional, Tuple, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from ..capability import CapabilitySpec

SUPPORTED_TYPES: Tuple[str, ...] = ("rpm", "tpm")


class RateLimits(BaseModel):
    """Per-scope ceilings for the current 60-second window.

    Shared by every policy type; the **unit** comes from the type. For
    `rpm` these are request counts, for `tpm` token counts — so the same
    number means wildly different things, and a limits document copied
    from one to the other is either trivially open or instantly closed.
    Nothing here can catch that, because both are valid documents; the
    tpm driver's `check_prerequisites()` warns when a token ceiling is
    small enough to look like a request ceiling.

    A request is denied if *any* applicable scope is over its limit.
    Zero means unlimited for that scope, not blocked — the inverse of
    what most people assume, which is why it is stated on every field.
    """

    model_config = ConfigDict(extra="forbid")

    user_default: int = Field(default=0, ge=0,
                              description="Per principal. 0 = unlimited.")
    model_default: int = Field(default=0, ge=0,
                               description="Per model. 0 = unlimited.")

    # Keyed by principal, which the gateway sets to the caller's bearer
    # token. So these maps contain credentials, and that is why the
    # driver puts the rendered document in a Secret rather than a
    # ConfigMap.
    user_overrides: Dict[str, int] = Field(default_factory=dict)
    model_overrides: Dict[str, int] = Field(default_factory=dict)
    # Keyed "<principal>|<model>". No default for this scope.
    user_model_overrides: Dict[str, int] = Field(default_factory=dict)

    @field_validator("user_overrides", "model_overrides",
                     "user_model_overrides")
    @classmethod
    def _limits_are_non_negative(cls, value: Dict[str, int], info):
        for key, limit in value.items():
            if not isinstance(limit, int) or isinstance(limit, bool):
                raise ValueError(
                    f"{info.field_name}[{key!r}] = {limit!r} is not an "
                    "integer. A limit is a count per minute — requests "
                    "for rpm, tokens for tpm."
                )
            if limit < 0:
                raise ValueError(
                    f"{info.field_name}[{key!r}] = {limit}. A limit cannot be "
                    "negative; 0 means unlimited for that scope."
                )
        return value

    @field_validator("user_model_overrides")
    @classmethod
    def _user_model_keys_are_pairs(cls, value: Dict[str, int]):
        for key in value:
            if "|" not in key:
                raise ValueError(
                    f"user_model_overrides key {key!r} must be "
                    "'<principal>|<model>'. Without the separator the "
                    "service never matches it and the limit silently does "
                    "nothing — use user_overrides or model_overrides for a "
                    "single scope."
                )
        return value


class RPMOptions(BaseModel):
    """Deployment settings specific to the rpm implementation."""

    model_config = ConfigDict(extra="forbid")

    release_name: str = "rpm"
    # A path while the chart is unpublished; an oci:// address once it is.
    chart: str = "api/microservices/rate-limiter-rpm/chart"
    chart_version: Optional[str] = None
    image_tag: Optional[str] = None

    # Seconds a request_id is remembered so at-least-once redelivery does
    # not double-count. Must cover the window it protects.
    dedupe_ttl: int = Field(default=120, ge=60)
    durable_name: str = "rpm-counter"

    def validate(self) -> None:
        if self.dedupe_ttl < 60:
            raise ValueError(
                f"dedupe_ttl={self.dedupe_ttl} is shorter than the 60s window "
                "it protects, so a redelivered event can be counted twice."
            )


class TPMOptions(BaseModel):
    """Deployment settings specific to the tpm implementation.

    TPM reads a different stream from RPM's raw `gateway.events` (ADR-030):
    the `enricher` service consumes that raw stream, guarantees a usable
    token count on every response event (falling back to the tokenizer
    itself, so TPM no longer needs one), and republishes to
    `gateway.events.enriched`. TPM subscribes to that derived stream --
    `event_stream_name`/`event_stream_subject` default to it, and the
    service enforces the pairing at startup: naming any other stream or
    subject there fails fast (a `RuntimeError`, not a silent miscount).

    `durable_name` also defaults to a *different* durable from RPM's, for
    a second, unrelated reason: both consumers read their own stream
    independently already, but the durable name is still what gives a
    replica its own cursor if this chart's replicaCount ever changes.
    Point both at one name and they split whichever stream they share
    between them, so each sees roughly half the events and both
    under-count, silently. The chart refuses `rpm-counter` outright for
    that reason.
    """

    model_config = ConfigDict(extra="forbid")

    release_name: str = "tpm"
    # A path while the chart is unpublished; an oci:// address once it is.
    chart: str = "api/microservices/rate-limiter-tpm/chart"
    chart_version: Optional[str] = None
    image_tag: Optional[str] = None

    # Seconds a request_id is remembered so at-least-once redelivery does
    # not double-count. Must cover the window it protects.
    dedupe_ttl: int = Field(default=120, ge=60)
    durable_name: str = "tpm-counter-enriched"

    # The enricher's derived stream (ADR-030 AD-07) -- the service itself
    # refuses to start against any other pairing, so these two travel
    # together; there is no reason to set one without the other.
    event_stream_name: str = "GATEWAY_EVENTS_ENRICHED"
    event_stream_subject: str = "gateway.events.enriched"

    def validate(self) -> None:
        if self.dedupe_ttl < 60:
            raise ValueError(
                f"dedupe_ttl={self.dedupe_ttl} is shorter than the 60s window "
                "it protects, so a redelivered event can be counted twice."
            )
        if self.durable_name == "rpm-counter":
            raise ValueError(
                "durable_name='rpm-counter' is the RPM limiter's durable.\n\n"
                "Both consumers read the same stream and subject, and the "
                "durable name is the only thing that gives each its own "
                "cursor. Sharing one splits the stream between them: each "
                "sees roughly half the events, both under-count, and neither "
                "reports an error."
            )


class Policy(CapabilitySpec):
    """A pre-request policy service on a Kubernetes cluster."""

    CAPABILITY: ClassVar[str] = "policy"
    SUPPORTED_TYPES: ClassVar[Tuple[str, ...]] = SUPPORTED_TYPES
    OPTIONS_FOR_TYPE: ClassVar[Dict[str, type]] = {
        "rpm": RPMOptions, "tpm": TPMOptions}
    DEFAULT_NAMESPACES: ClassVar[Dict[str, str]] = {
        "rpm": "policy", "tpm": "policy"}
    REQUIRES: ClassVar[Tuple[str, ...]] = ("cluster",)

    FROM_STACK: ClassVar[Dict[str, str]] = {
        "kubeconfig_path": "kubeconfig_path",
        "cache_url": "cache_url",
        "event_backbone_url": "event_backbone_url",
    }
    # What the gateway needs to attach the chain.
    PROVIDES: ClassVar[Dict[str, str]] = {"policy_endpoint": "endpoint"}

    type: str = "rpm"
    kubeconfig_path: str
    namespace: Optional[str] = None
    options: Optional[Union[RPMOptions, TPMOptions]] = None

    limits: RateLimits = Field(default_factory=RateLimits)

    # Where the counters live. Required, because the alternative is the
    # service's own default of localhost, which in a pod means no Valkey
    # at all — and it fails open, so nothing is limited and nothing says so.
    #
    # Credential-free: this renders into the chart's ConfigMap, and a
    # password there is readable by anything holding `get configmaps` in
    # this namespace. `cache_auth_url` below is where one goes.
    cache_url: str

    # The same URL with its password, for a Valkey that has auth enabled.
    # Rendered into the chart's own Secret, which envFrom applies *after*
    # the ConfigMap — so this overrides `cache_url` inside the pod while
    # leaving the ConfigMap credential-free.
    #
    # SecretStr, like DatabaseConfig.password: pydantic masks it in repr()
    # and model_dump(), so a spec reaching a traceback or a bare print()
    # shows `SecretStr('**********')`. `.get_secret_value()` is the one
    # place the real value appears, and it greps.
    #
    # None leaves the ConfigMap's value standing, which is correct for a
    # Valkey with auth disabled. It never reaches a command line: the Helm
    # layer passes values to helm on stdin, never through --set.
    cache_auth_url: Optional[SecretStr] = None

    # Per-org plan limits (AD-04): resolved from admin-control-plane's
    # GET /internal/v1/organizations/{id}/plan instead of the static
    # `limits.user_default`/`user_overrides` above, when reachable. Empty
    # means inert -- falls back to those static values exactly as if
    # this field never existed, so leaving it unset is not a degraded
    # state, just the pre-AD-04 behavior.
    admin_cp_url: str = ""

    # Bearer for that endpoint. A credential, so SecretStr like
    # `cache_auth_url` above, and rendered into the chart's own Secret
    # rather than its ConfigMap. Must equal admin-control-plane's own
    # SERVICE_API_KEY exactly, or the lookup 401s and this silently falls
    # back to the static limits -- the same failure shape a mismatched
    # `cache_auth_url` has, just against a different upstream.
    admin_cp_service_api_key: Optional[SecretStr] = None

    # Counting is asynchronous: /check only reads the counters, and a
    # consumer increments them from the gateway's events. Empty means no
    # consumer, so the counters never advance and every request is
    # allowed. See the validator below.
    event_backbone_url: str = ""
    allow_no_backbone: bool = False

    replicas: int = Field(default=1, ge=1, le=1)

    # matchLabels selectors (e.g. [{"app": "model-gateway"}]) allowed to
    # reach this service's /check on its port. Empty => the chart installs
    # no NetworkPolicy, so an existing deployment is untouched until a
    # caller opts in (ADR-006: features attach by config, not code).
    network_policy_allowed_ingress: List[Dict[str, str]] = Field(default_factory=list)

    # Scheduling. There is no in-cluster registry, so a service image
    # exists only on the node it was imported to, and a pod scheduled
    # anywhere else stays ImagePullBackOff with nothing in the release to
    # explain it. The chart's own affinity already keeps pods off the
    # control plane; this is how a caller says *which* worker.
    node_selector: Optional[Dict[str, str]] = None
    tolerations: Optional[List[Dict[str, Any]]] = None

    @property
    def service_name(self) -> str:
        """The Service the chart creates, which is also the Deployment name.

        The chart's fullname template collapses `<release>-<chart>` to just
        the release when the release name already contains the chart name,
        so a release called `rate-limiter-tpm` is not
        `rate-limiter-tpm-rate-limiter-tpm`. Mirrored here rather than
        assumed, because `endpoint` is built from it and a wrong name
        sends the gateway's policy chain to a Service that does not exist
        — which fails open or closed depending on POLICY_FAIL_MODE, and
        neither is the intended behaviour.
        """
        chart_name = f"rate-limiter-{self.type}"
        release = self.options.release_name if self.options else self.type
        if chart_name in release:
            return release
        return f"{release}-{chart_name}"

    @property
    def endpoint(self) -> str:
        """The URL the gateway puts in POLICY_ENDPOINTS."""
        return (f"http://{self.service_name}.{self.resolved_namespace}"
                ".svc:8000/check")

    def validate(self) -> None:
        self.validate_capability()
        if not self.event_backbone_url and not self.allow_no_backbone:
            raise ValueError(
                "event_backbone_url is empty, so nothing would count.\n\n"
                "Counting is asynchronous: /check only reads the counters, "
                "and a JetStream consumer increments them from the gateway's "
                "request events. With no backbone there is no consumer, the "
                "counters never advance, and /check allows every request — a "
                "limiter that is deployed, healthy, and enforcing nothing.\n\n"
                "Set event_backbone_url, or allow_no_backbone=True to deploy "
                "an always-allow /check deliberately (useful for wiring the "
                "gateway up before the backbone exists)."
            )

    @model_validator(mode="after")
    def _validate_on_construction(self):
        """Runs validate() at construction, and again on any assignment.

        Without this the capability's own rules only run when the backend
        dispatches, so a spec could exist for minutes in a state it would
        later reject. `validate()` stays public because driver_for() calls
        it and it reads well at a call site — it just is not the first
        place a problem shows up.
        """
        self.validate()
        return self
