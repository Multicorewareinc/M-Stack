---
name: policy
description: Use this skill when the user's request mentions rate limiting, request quotas, requests-per-minute, tokens-per-minute, throttling, or a policy chain in front of a model gateway. Covers the real MultiStack SDK Policy/RateLimits/RPMOptions/TPMOptions/PolicyBackend classes.
---

# MultiStack SDK — Policy / RateLimits / RPMOptions / TPMOptions / PolicyBackend

Real class signatures (from `multistack.policy`, re-exported at the top
level as `from multistack import Policy, PolicyBackend, RateLimits` —
note `RPMOptions`/`TPMOptions` themselves are only at the
`multistack.policy` submodule level, not top-level). Policy installs
*into an existing cluster* — it does not create one. There are two
supported implementations: "rpm" counts requests, "tpm" counts tokens —
same contract, different counter, same `RateLimits` document (the unit
just changes meaning). Either attaches to a Gateway by configuration
alone (the gateway's `policy_endpoints`), never by changing the gateway
image.

`Policy`/`RateLimits`/`RPMOptions`/`TPMOptions` are Pydantic models, and
`build_policy_plan`'s `policy` parameter is typed as `Policy` directly —
the field-by-field shape below is generated straight from these classes
for the tool schema a model actually receives, not retyped by hand.

```python
class RateLimits(BaseModel):
    # A request is denied if ANY applicable scope is over its limit in
    # the current 60-second window. 0 = UNLIMITED for that scope, not
    # blocked -- the inverse of what most people assume.
    user_default: int = 0            # per principal (the caller's bearer token). 0 = unlimited.
    model_default: int = 0           # per model. 0 = unlimited.
    user_overrides: dict = {}        # {principal: limit} -- contains credentials, stored in a Secret, not a ConfigMap
    model_overrides: dict = {}       # {model: limit}
    user_model_overrides: dict = {}  # {"<principal>|<model>": limit} -- key MUST contain "|" or the limit silently does nothing

class RPMOptions(BaseModel):
    release_name: str = "rpm"
    chart: str = "api/microservices/rate-limiter-rpm/chart"
    chart_version: Optional[str] = None
    image_tag: Optional[str] = None
    dedupe_ttl: int = 120             # >= 60 seconds -- must cover the window it protects, or a redelivered event double-counts
    durable_name: str = "rpm-counter"

class TPMOptions(BaseModel):
    release_name: str = "tpm"
    chart: str = "api/microservices/rate-limiter-tpm/chart"
    chart_version: Optional[str] = None
    image_tag: Optional[str] = None
    dedupe_ttl: int = 120              # >= 60 seconds, same reasoning as RPMOptions
    # tpm reads a DIFFERENT stream from rpm. The enricher guarantees a
    # token count and republishes to gateway.events.enriched; tpm consumes
    # that, so it never calls a tokenizer itself (ADR-030).
    durable_name: str = "tpm-counter-enriched"
    event_stream_name: str = "GATEWAY_EVENTS_ENRICHED"
    event_stream_subject: str = "gateway.events.enriched"

class Policy(BaseModel):
    type: str = "rpm"                 # or "tpm" -- always explicit, never guess which the user wants
    kubeconfig_path: str              # required -- the EXISTING cluster this installs onto
    namespace: Optional[str] = None
    options: Optional[Union[RPMOptions, TPMOptions]] = None   # must match `type` -- RPMOptions for "rpm", TPMOptions for "tpm"

    limits: RateLimits = RateLimits()

    # Where the counters live. Required -- the service's own fallback is
    # localhost, which in a pod means no Redis at all, and it fails
    # open (nothing limited, nothing says so).
    cache_url: str

    # The SAME url WITH its password, for a cache that has auth enabled.
    # Optional[SecretStr]: it renders into the chart's Secret rather than
    # its ConfigMap, so the credential-free cache_url above stays safe to
    # put in a ConfigMap. None is correct ONLY for a cache with auth
    # disabled -- see the rule below, this one fails open.
    cache_auth_url: Optional[SecretStr] = None

    # Per-org plan limits, read from the admin control plane instead of
    # the static `limits` below. Empty keeps the static limits.
    admin_cp_url: str = ""
    # The admin control plane's own SERVICE_API_KEY. Secret, like
    # cache_auth_url. Required whenever admin_cp_url is set.
    admin_cp_service_api_key: Optional[SecretStr] = None

    # Counting is asynchronous: /check only reads the counters, and a
    # JetStream consumer increments them from the gateway's events.
    # Empty means no consumer, so counters never advance and /check
    # allows everything.
    event_backbone_url: str = ""
    allow_no_backbone: bool = False   # explicit opt-in to deploy an always-allow /check anyway

    replicas: int = 1                 # fixed at 1 today (ge=1, le=1)

    # matchLabels selectors allowed to reach this service's /check
    # (e.g. [{"app": "model-gateway"}]). Empty => NO NetworkPolicy is
    # installed at all, so an existing deployment is untouched until a
    # caller opts in.
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
        # - event_backbone_url empty AND allow_no_backbone is not True

class PolicyBackend:
    def create(self, policy: Policy) -> str: ...   # returns the policy's /check endpoint URL
    def delete(self, policy: Policy) -> None: ...
    def check_prerequisites(self, policy: Policy) -> None: ...
    def enforcing(self, policy: Policy) -> bool: ...
```

Rules when generating code:
- `kubeconfig_path` is required and has no ambient fallback — the SDK deliberately never reads `$KUBECONFIG`/`~/.kube/config`, so a stale default can't silently target the wrong cluster.
- `cache_url` is required and has no safe default — leaving it unset is not an option; ask the user for it if they haven't given one (typically a Valkey/Redis endpoint). If a Valkey deployment is being built in the same whole-stack request (see `build_full_stack_plan`), its real endpoint is wired into `cache_url` automatically — see the `valkey` skill.
- `event_backbone_url` is required **unless** the user explicitly wants an always-allow policy deployed before the backbone exists — never set `allow_no_backbone=True` on your own judgment. It defeats the entire purpose of deploying this (the limiter would be healthy and enforcing nothing), so only set it when the user explicitly asks for that specific behavior (e.g. "deploy the gateway wiring now, backbone comes later").
- In `limits`, 0 means UNLIMITED, not blocked. Leave a scope at its 0 default rather than guessing a number the user didn't give — don't invent a "reasonable" rate limit.
- `type="rpm"` limits **requests**; `type="tpm"` limits **tokens** — the same `RateLimits` numbers mean wildly different things depending on `type`, so never copy a limits document from one type to the other without the user confirming the numbers still make sense for the new unit.
- For `type="tpm"`, never set `options.durable_name` to `"rpm-counter"` — the real SDK rejects it. The two limiters now read different streams entirely (rpm the raw `gateway.events`, tpm the enriched one), so leave `durable_name`, `event_stream_name` and `event_stream_subject` at their defaults unless the user has a specific reason to change them.
- `user_model_overrides` keys must be `"<principal>|<model>"` — a key without the `|` separator is accepted by validation shape-wise but the service never matches it, so use `user_overrides` or `model_overrides` instead for a single-scope override.
- Policy depends on a cluster (`REQUIRES = ("cluster",)`) — don't propose installing a policy before a cluster exists.
- Once deployed, this Policy's `endpoint` is what goes into a Gateway's `policy_endpoints` (see the `gateway` skill / `build_gateway_plan`) to actually attach the chain — deploying a Policy alone does nothing until a Gateway references it.
- **`cache_auth_url` is a real credential and is never filled in for you.** The Valkey/Bitnami chart turns auth ON by default, so a limiter deployed against a real cache with `cache_auth_url` unset gets `NOAUTH` on every request — and a rate limiter that cannot read its counters **fails open**: deployed, Ready, both probes green, enforcing nothing. Leave it unset only when the user has explicitly said the cache has auth disabled. Otherwise ask them for it — it is the cache URL with the password in it, and it must never be invented, nor put in `cache_url` (that one renders into a ConfigMap, readable by anything with `get configmaps` in the namespace).
- **`admin_cp_url` / `admin_cp_service_api_key` switch the limits to per-org plans** read from the admin control plane. Leave both unset unless the user asks for per-org or plan-based limits — unset is the supported static-limits behaviour, not a degraded one. The key is a real credential: it must equal the admin control plane's own `SERVICE_API_KEY`, so ask the user for it and never invent it. A URL without the key is rejected, because the lookup would 401 and silently fall back to the static limits. `admin_cp_url` is the admin ControlPlane's `endpoint`; `build_full_stack_plan` fills it in when an admin control plane is composed and the key was given.
- **`type="tpm"` no longer calls a tokenizer.** ADR-030 moved token counting into the Enricher, which guarantees a count and republishes to `gateway.events.enriched`; tpm consumes that enriched stream. `TPMOptions.tokenizer_url`/`tokenizer_timeout_ms` were **deleted** from the SDK — passing either now raises `extra_forbidden`, so never construct them, and don't offer a tokenizer as something that wires into a policy.
- **A `type="tpm"` policy needs an Enricher already running on the cluster.** By default it reads `gateway.events.enriched` (`TPMOptions.event_stream_subject`), and nothing but the Enricher publishes that stream (see the `enricher` skill). Without one, it is deployed, Ready, and **fails open**: every request is silently allowed, since no count ever arrives. This tool has no way to check whether an enricher already exists on a target cluster the way `build_full_stack_plan` can check a same-call composition, so if the user is deploying a fresh `type="tpm"` policy, ask whether an enricher is already running rather than assuming one is. `type="rpm"` has no such dependency — it reads the raw `gateway.events` stream directly.
- `network_policy_allowed_ingress` locks down who may reach this service's `/check`, and it is **off by default**: an empty list installs no NetworkPolicy at all. Only set it when the user asks to restrict ingress, with real selector labels they gave you (typically the Model Gateway's, e.g. `[{"app": "model-gateway"}]`) — naming the wrong one cuts the gateway off from the limiter it depends on.
- `node_selector`/`tolerations` pin this service to a particular node. They matter here because there is no in-cluster registry: a service image exists only on the node it was imported to, so a pod scheduled anywhere else sits in `ImagePullBackOff` with nothing in the release to explain it. Leave both unset unless the user tells you which node holds the image — a node name or label is a real fact about their cluster, never one to invent. Note `None` and `{}` are different statements: unset leaves the chart's own default in place, while an explicit `{}` overrides it with "schedule anywhere".
