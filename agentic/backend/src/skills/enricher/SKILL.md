---
name: enricher
description: Use this skill when the user's request mentions an enricher, token counting on events, enriched events, or guaranteeing a usage count for billing and token limits. Covers the real MultiStack SDK Enricher/EnricherBackend classes.
---

# MultiStack SDK — Enricher / EnricherBackend

Real class signature (from `multistack`, re-exported at the top level as
`from multistack import Enricher, EnricherBackend`). The Enricher sits
between the gateway's raw event stream and everything downstream that
needs a token count off it.

It consumes `gateway.events`, falls back to the Tokenizer when a
response carries no `usage` block, and republishes to a **derived**
stream (`gateway.events.enriched`) whose retention and cursor stay
independent of the raw input. This is what makes ADR-030 true: a
`type="tpm"` rate limiter and `billing` both read the derived stream
rather than calling a tokenizer themselves.

```python
class EnricherOptions(BaseModel):
    release_name: str = "enricher"
    chart: str = "api/microservices/enricher/chart"
    chart_version: Optional[str] = None
    image_tag: Optional[str] = None
    event_stream_name: str = "GATEWAY_EVENTS"       # the RAW stream it consumes
    event_stream_subject: str = "gateway.events"
    durable_name: str = "enricher"
    enriched_stream_name: str = "GATEWAY_EVENTS_ENRICHED"   # the DERIVED stream it publishes
    enriched_subject: str = "gateway.events.enriched"
    max_deliver: int = 5
    ack_wait_seconds: int = 30
    tokenizer_url: str = ""          # the Tokenizer's endpoint -- see rules
    tokenizer_timeout_ms: int = 2000

class Enricher(CapabilitySpec):
    type: str = "enricher"            # the only implementation
    kubeconfig_path: str              # required -- the EXISTING cluster
    namespace: Optional[str] = None   # defaults to "policy"
    options: Optional[EnricherOptions] = None
    service_port: int = 8000
    event_backbone_url: str = ""      # required unless allow_no_backbone
    allow_no_backbone: bool = False
    replicas: int = 1                 # at most 1 -- see rules
    node_selector: Optional[Dict[str, str]] = None
    tolerations: Optional[List[Dict[str, Any]]] = None
```

Rules when generating code:
- Every field above except `kubeconfig_path` has a default. Leave them at it unless the user asks for something specific -- never ask the user for ports, retry counts or timeouts.
- `replicas` must be 1: the SDK rejects any other value (`ge=1, le=1`).
- `kubeconfig_path` is required and has no ambient fallback — the SDK deliberately never reads `$KUBECONFIG`/`~/.kube/config`.
- `event_backbone_url` is required **unless** `allow_no_backbone=True` is explicitly set. The enricher exists to consume one stream and publish another, so with no backbone it is deployed, healthy, and doing nothing — and the things downstream of it (a tpm limiter, and billing) then count **nothing at all** rather than erroring. Never set `allow_no_backbone=True` on your own judgement; only when the user explicitly asks to deploy ahead of the backbone.
- **The Enricher is the Tokenizer's consumer, not the rate limiter.** Put the Tokenizer's endpoint in `options.tokenizer_url` (see the `tokenizer` skill). As of ADR-030, `TPMOptions.tokenizer_url` was **deleted** from the SDK — never try to wire a tokenizer into a policy.
- **Without an Enricher deployed, a `type="tpm"` limiter and `billing` both go quietly uncounted** — neither errors, they just never see an enriched event. If the user is deploying either of those, say that an enricher is needed for them to count anything.
- This capability publishes no address — TPM and billing reach its output through NATS stream/subject strings, which are plain configuration, not a Service to connect to.
