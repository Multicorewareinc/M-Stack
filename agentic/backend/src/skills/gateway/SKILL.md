---
name: gateway
description: Use this skill when the user's request mentions a model gateway, inference gateway, an authenticated front door to a model, API keys for inference, or routing requests to vLLM/other models. Covers the real MultiStack SDK Gateway/ModelGatewayOptions/GatewayBackend classes.
---

# MultiStack SDK — Gateway / ModelGatewayOptions / GatewayBackend

Real class signatures (from `multistack.gateway`, re-exported at the top
level as `from multistack import Gateway, GatewayBackend` — note
`ModelGatewayOptions` itself is only at the `multistack.gateway`
submodule level, not top-level). Gateway installs *into an existing
cluster* — it does not create one. "modelgateway" is the only
supported implementation today.

`Gateway`/`ModelGatewayOptions` are Pydantic models, and
`build_gateway_plan`'s `gateway` parameter is typed as `Gateway`
directly — the field-by-field shape below is generated straight from
these classes for the tool schema a model actually receives, not
retyped by hand.

```python
class ModelGatewayOptions(BaseModel):
    release_name: str = "gateway"
    chart: str = "api/microservices/model-gateway/chart"
    chart_version: Optional[str] = None
    image_tag: Optional[str] = None
    event_stream_name: str = "GATEWAY_EVENTS"
    event_stream_subject: str = "gateway.events"
    capture_bodies: bool = False        # emits full prompt/response text in events -- a governance decision
    max_inflight: int = 1000            # >= 1

class Gateway(BaseModel):
    type: str = "modelgateway"
    kubeconfig_path: str                # required -- the EXISTING cluster this installs onto
    namespace: Optional[str] = None
    options: Optional[ModelGatewayOptions] = None   # auto-filled from `type` if left unset

    upstream_url: str                   # required -- the OpenAI-compatible server to proxy to, typically an Inference deployment's endpoint

    # The NAME of a Kubernetes Secret already holding API_KEY (and
    # optionally UPSTREAM_API_KEY / MODEL_ROUTES) -- never the real key
    # itself. Required: the gateway image ships a default development
    # key, so deploying without a Secret would put a published
    # credential in front of the upstream.
    api_key_secret: str

    # Where the gateway verifies every /v1 bearer token: the ORGANIZATION
    # control plane's in-cluster URL (it calls
    # GET /internal/v1/api-keys/verify). Required, no default, no
    # degraded mode -- the static-key auth path was removed, so a gateway
    # without this passes both probes and then 503s every real request
    # with "key verification is unavailable".
    org_cp_internal_url: str

    model_routes: dict = {}             # {model_name: plain http(s) URL} -- no credentials allowed here, see rules below
    policy_endpoints: list = []         # empty means no rate-limiting chain (inert)
    policy_timeout_ms: int = 500        # >= 100 once policy_endpoints is non-empty
    policy_fail_mode: str = "closed"    # "closed" or "open"
    request_timeout: float = 60.0
    event_backbone_url: str = ""        # empty means no event publisher built
    replicas: int = 2                   # >= 1
    service_port: int = 8080

    # matchLabels selectors allowed to reach this gateway (e.g. the Istio
    # ingress gateway's own pods). Empty => NO NetworkPolicy is installed
    # at all, so an existing deployment is untouched until a caller opts in.
    network_policy_allowed_ingress: List[Dict[str, str]] = []
    # Scheduling. There is no in-cluster registry, so a service image
    # exists only on the node it was imported to -- a pod scheduled
    # anywhere else sits in ImagePullBackOff with nothing in the release
    # to explain it. None (the default) leaves the chart's own default in
    # place; an explicit {} OVERRIDES it with "schedule anywhere", which
    # is a different statement.
    node_selector: Optional[Dict[str, str]] = None
    tolerations: Optional[List[Dict[str, Any]]] = None

    def validate(self) -> None: ...   # raises ValueError on:
        # - policy_timeout_ms < 100 while policy_endpoints is non-empty (measured 62ms cold / 32ms warm for a real /check round-trip)
        # - a policy_endpoints entry that isn't an http(s) URL
        # - policy_fail_mode not "closed" or "open"
        # - a model_routes entry that isn't a plain http(s) URL string (a nested/credentialed value is rejected before coercion, with a message pointing at api_key_secret)

class GatewayBackend:
    def create(self, gateway: Gateway) -> str: ...   # returns the gateway's endpoint URL
    def delete(self, gateway: Gateway) -> None: ...
    def check_prerequisites(self, gateway: Gateway) -> None: ...
```

Rules when generating code:
- `kubeconfig_path` is required and has no ambient fallback — the SDK deliberately never reads `$KUBECONFIG`/`~/.kube/config`, so a stale default can't silently target the wrong cluster.
- `build_gateway_plan` is for adding a gateway to a cluster that **already exists** — it needs a real `kubeconfig_path`. If the cluster is also being created in this same request, use `build_full_stack_plan` instead.
- Gateway depends on a cluster (`REQUIRES = ("cluster",)`) — don't propose installing a gateway before a cluster exists.
- `org_cp_internal_url` is **required**, and must be the **organization** control plane's in-cluster URL (`type="organization"` — not the admin one), since that is the service the gateway asks to verify each bearer token. There is no default and no degraded mode: without it every `/v1` call returns 503 "key verification is unavailable" while both probes stay green, so the deployment looks healthy and serves nothing. It is deliberately **not** auto-wired from a composed control plane, even in `build_full_stack_plan` — a Stack records whichever control plane was recorded last and cannot tell an admin one from an organization one. Always a real value the user supplies; ask if they haven't given one, never guess an address.
- **This is the platform's own documented baseline, and `build_full_stack_plan` enforces it as a hard requirement**: composing `gateway` there without also composing an `organization`-type `controlplane` is rejected outright, not just under-documented — a gateway genuinely cannot serve a request without one. Standalone `build_gateway_plan` can't check whether an organization control plane already exists on the target cluster the way the whole-stack tool can check a same-call composition, so when building a gateway alone, confirm with the user that one is already running (see the `controlplane` skill) rather than assuming it. This baseline cascades further still: an organization control plane itself requires a real database and cache, so "gateway alone" in practice means four real things exist — Postgres, Redis, the organization control plane, and the gateway — not one.
- The Secret named by `api_key_secret` must also carry `ORG_VERIFY_API_KEY`, and its value has to equal the organization control plane's own `MG_SERVICE_API_KEY` exactly — the two Secrets are created separately and nothing but convention keeps them in step. Worth stating when you hand back a gateway plan; never invent either value.
- `api_key_secret` is always a Secret **name**, never a real API key — never put an actual credential value into this field or into `model_routes`. If a route needs its own provider credential, that whole `MODEL_ROUTES` document belongs inside the Secret named by `api_key_secret` (it overrides the plain values applied first), not in the spec itself.
- Leave `policy_endpoints` empty for a plain authenticated proxy with no rate limiting — only set it once a Policy has actually been deployed (see the `policy` skill / `build_policy_plan`) and its real `endpoint` is known. Once you do set it, don't drop `policy_timeout_ms` below 100. **In `build_full_stack_plan`, this is automatic**: if a request builds a gateway together with one or more policies in the same call, their real endpoints are wired into `policy_endpoints` for you — don't guess a URL for it there, and don't ask the user for one either.
- **Similarly, `upstream_url` wires itself in `build_full_stack_plan`** when the same call also builds an Inference deployment — the real endpoint is used automatically, so leave `upstream_url` as any placeholder string in that specific case (the whole-stack tool documents exactly which fields are placeholders and why). Outside that tool, or when Inference isn't part of the same request, `upstream_url` is always a real value: the address of whatever OpenAI-compatible server this gateway should proxy to.
- Leave `options` unset unless the user describes a specific need (a non-default release name, pinning a chart version, capturing request/response bodies for auditing) — `capture_bodies=True` carries real user content into events, so only turn it on if the user explicitly asks.
- The gateway's `endpoint` this produces is what a client calls, and is also what should be handed to another Gateway/service as an upstream if ever chained.
- `network_policy_allowed_ingress` locks down who may reach this gateway, and it is **off by default**: an empty list installs no NetworkPolicy at all, leaving an existing deployment untouched. Only set it when the user asks to restrict ingress, and only with real selector labels they gave you (e.g. the Istio ingress gateway's own pods) — a wrong selector silently cuts off every legitimate caller, since the policy denies whatever it doesn't name.
- `node_selector`/`tolerations` pin this service to a particular node. They matter here because there is no in-cluster registry: a service image exists only on the node it was imported to, so a pod scheduled anywhere else sits in `ImagePullBackOff` with nothing in the release to explain it. Leave both unset unless the user tells you which node holds the image — a node name or label is a real fact about their cluster, never one to invent. Note `None` and `{}` are different statements: unset leaves the chart's own default in place, while an explicit `{}` overrides it with "schedule anywhere".
