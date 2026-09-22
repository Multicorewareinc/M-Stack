"""The gateway capability: the authenticated front door to inference.

    Gateway(type="modelgateway", kubeconfig_path=kc,
            upstream_url=..., api_key_secret="gateway-keys",
            org_cp_internal_url="http://organization-control-plane...:8000")

Authentication, a pluggable policy chain, and the event backbone, in
front of an OpenAI-compatible upstream. `modelgateway` is the
first-party implementation; a LiteLLM or Envoy-based one would be
another `type` behind the same spec.

`org_cp_internal_url` is required, not optional configuration: ADR-026
removed the gateway's static-key auth mode, so every `/v1` request now
authenticates by asking the organization control plane to resolve the
caller's bearer. A Gateway built without it would deploy, pass both
probes, and 503 the first real request -- exactly what discovered this
field was missing, in an end-to-end run against a live deployment. The
corresponding credential, `ORG_VERIFY_API_KEY`, is not a spec field: it
travels in the Secret named by `api_key_secret`, alongside `API_KEY`,
the same way every other credential here does.

Everything past proxying attaches by configuration rather than code
(ADR-006): an empty `policy_endpoints` builds no policy client and an
empty `event_backbone_url` builds no publisher, so the defaults here
deploy a plain authenticated proxy and each feature is one field away.
"""
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from ..capability import CapabilitySpec

SUPPORTED_TYPES: Tuple[str, ...] = ("modelgateway",)

FAIL_MODES: Tuple[str, ...] = ("closed", "open")

# Every key the chart's Secret must carry. Named here, not just in the
# chart's own `required` guards, so check_prerequisites can say which one
# an existingSecret is missing by name -- the chart cannot inspect an
# existingSecret's contents, so a missing key installs cleanly and passes
# both probes, and only the first real request reveals it.
REQUIRED_SECRET_KEYS: Tuple[str, ...] = ("API_KEY", "ORG_VERIFY_API_KEY")


class ModelGatewayOptions(BaseModel):
    """Deployment settings specific to the first-party gateway."""

    model_config = ConfigDict(extra="forbid")

    release_name: str = "gateway"
    chart: str = "api/microservices/model-gateway/chart"
    chart_version: Optional[str] = None
    image_tag: Optional[str] = None

    event_stream_name: str = "GATEWAY_EVENTS"
    event_stream_subject: str = "gateway.events"
    # Emitting full prompt and response text is a governance decision,
    # not a performance one: those events carry user content.
    capture_bodies: bool = False
    max_inflight: int = Field(default=1000, ge=1)

    def validate(self) -> None:
        return None


class Gateway(CapabilitySpec):
    """An authenticated OpenAI-compatible gateway on a Kubernetes cluster."""

    CAPABILITY: ClassVar[str] = "gateway"
    SUPPORTED_TYPES: ClassVar[Tuple[str, ...]] = SUPPORTED_TYPES
    OPTIONS_FOR_TYPE: ClassVar[Dict[str, type]] = {
        "modelgateway": ModelGatewayOptions}
    DEFAULT_NAMESPACES: ClassVar[Dict[str, str]] = {"modelgateway": "gateway"}
    REQUIRES: ClassVar[Tuple[str, ...]] = ("cluster",)

    FROM_STACK: ClassVar[Dict[str, str]] = {
        "kubeconfig_path": "kubeconfig_path",
        "event_backbone_url": "event_backbone_url",
        "api_key_secret": "api_key_secret",
    }
    PROVIDES: ClassVar[Dict[str, str]] = {"gateway_endpoint": "endpoint"}

    type: str = "modelgateway"
    kubeconfig_path: str
    namespace: Optional[str] = None
    options: Optional[ModelGatewayOptions] = None

    # The OpenAI-compatible server to proxy to.
    upstream_url: str

    # The Secret holding API_KEY, and optionally UPSTREAM_API_KEY and a
    # MODEL_ROUTES that carries per-provider credentials. Required, and a
    # name rather than a value: the gateway image ships a default
    # development key, so deploying without a Secret would put a
    # published credential in front of the upstream.
    api_key_secret: str

    # Where /v1 auth verifies a bearer against the org that owns it.
    # Required: ADR-026 removed the static-key fallback, so with no
    # organization control plane to call, verify_api_key's own transport
    # error becomes a 503 on every request -- there is no degraded mode.
    # Not in FROM_STACK: a Stack holding both control-plane types
    # publishes only the last-recorded controlplane_endpoint, which is
    # not necessarily the organization one, so this is wired the same
    # explicit way examples/portal/install.py wires api_upstream --
    # derive it from a ControlPlane(type="organization", ...) spec's own
    # `.endpoint` rather than typing an address out.
    org_cp_internal_url: str

    # Routes with no credentials in them. A route that needs a provider's
    # own api_key goes in the Secret instead — see the validator.
    model_routes: Dict[str, str] = Field(default_factory=dict)

    # Policy services implementing POST /check. Empty means inert.
    policy_endpoints: List[str] = Field(default_factory=list)

    # 500ms, not the 50 the service itself defaults to. Measured from
    # inside the gateway pod, a /check to the limiter in the same cluster
    # took 62ms cold and 32ms warm, so 50 was below the floor and the
    # first request of every idle period failed closed with a 503.
    policy_timeout_ms: int = Field(default=500, ge=1)

    # Closed is right for a limiter: a policy that errors should block,
    # because failing open under load is exactly when the limit matters.
    policy_fail_mode: str = "closed"

    request_timeout: float = Field(default=60.0, gt=0)
    event_backbone_url: str = ""
    replicas: int = Field(default=2, ge=1)
    service_port: int = Field(default=8080, gt=0, lt=65536)

    # matchLabels selectors (e.g. the Istio ingress gateway's own pods)
    # allowed to reach this gateway. Empty => the chart installs no
    # NetworkPolicy, so an existing deployment is untouched until a caller
    # opts in (ADR-006: features attach by config, not code).
    network_policy_allowed_ingress: List[Dict[str, str]] = Field(default_factory=list)

    # Scheduling. There is no in-cluster registry, so a service image
    # exists only on the node it was imported to, and a pod scheduled
    # anywhere else stays ImagePullBackOff with nothing in the release to
    # explain it. The chart's own affinity already keeps pods off the
    # control plane; this is how a caller says *which* worker.
    node_selector: Optional[Dict[str, str]] = None
    tolerations: Optional[List[Dict[str, Any]]] = None

    @field_validator("policy_fail_mode")
    @classmethod
    def _known_fail_mode(cls, value: str) -> str:
        if value not in FAIL_MODES:
            raise ValueError(
                f"policy_fail_mode={value!r} is not one of {FAIL_MODES}. "
                "'closed' blocks a request when a policy errors; 'open' "
                "allows it through."
            )
        return value

    @field_validator("model_routes", mode="before")
    @classmethod
    def _routes_carry_no_credentials(cls, value: Any):
        """Runs before coercion, so this message survives.

        `Dict[str, str]` would reject a nested route on its own, but with
        "Input should be a valid string" — which names the type mismatch
        and not the thing the caller needs to know, which is that the
        document belongs in the Secret. Same reason capability.py
        validates `options` before coercion.
        """
        if not isinstance(value, dict):
            return value                    # let pydantic report the shape
        for model, route in value.items():
            if not isinstance(route, str):
                raise ValueError(
                    f"model_routes[{model!r}] is not a URL. A route that "
                    "needs its provider's api_key cannot live in a spec — "
                    "put the whole MODEL_ROUTES document in the Secret named "
                    "by api_key_secret, which the deployment applies after "
                    "the plain values and which therefore overrides them."
                )
            if not route.startswith(("http://", "https://")):
                raise ValueError(
                    f"model_routes[{model!r}] = {route!r} is not an http(s) "
                    "URL."
                )
        return value

    @property
    def service_name(self) -> str:
        release = self.options.release_name if self.options else "gateway"
        return f"{release}-model-gateway"

    @property
    def endpoint(self) -> str:
        return (f"http://{self.service_name}.{self.resolved_namespace}"
                f".svc:{self.service_port}")

    def validate(self) -> None:
        self.validate_capability()
        if self.policy_endpoints and self.policy_timeout_ms < 100:
            raise ValueError(
                f"policy_timeout_ms={self.policy_timeout_ms} with a policy "
                "chain configured. A /check across a pod hop plus the policy "
                "service's own datastore round-trip measured 62ms cold and "
                "32ms warm on a local cluster, so anything under 100ms means "
                "the first request of every idle period exceeds it — and "
                "with fail_mode 'closed' that is a 503 to the client. Raise "
                "it, or drop policy_endpoints if you did not want a chain."
            )
        for endpoint in self.policy_endpoints:
            if not endpoint.startswith(("http://", "https://")):
                raise ValueError(
                    f"policy_endpoint {endpoint!r} is not an http(s) URL."
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
