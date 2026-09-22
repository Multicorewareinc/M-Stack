---
name: tokenizer
description: Use this skill when the user's request mentions a tokenizer, token counting, tiktoken, or usage/quota accounting for model responses. Covers the real MultiStack SDK Tokenizer/TokenizerBackend classes.
---

# MultiStack SDK — Tokenizer / TokenizerBackend

Real class signature (from `multistack`, re-exported at the top level as
`from multistack import Tokenizer, TokenizerBackend`). A small,
unauthenticated HTTP service that counts tokens for a model's encoding —
it exists so the Policy layer's `type="tpm"` driver can answer "how many
tokens was that?" for a response the upstream did not report `usage`
for.

```python
class TiktokenOptions(BaseModel):
    release_name: str = "tokenizer"
    chart: str = "api/microservices/tokenizer/chart"
    chart_version: Optional[str] = None
    image_tag: Optional[str] = None
    default_encoding: str = "cl100k_base"

class Tokenizer(CapabilitySpec):
    type: str = "tiktoken"              # the only implementation today
    kubeconfig_path: str                # required -- the EXISTING cluster
    namespace: Optional[str] = None
    options: Optional[TiktokenOptions] = None
    replicas: int = 2
    service_port: int = 8000
    # Scheduling. There is no in-cluster registry, so a service image
    # exists only on the node it was imported to -- a pod scheduled
    # anywhere else sits in ImagePullBackOff with nothing in the release
    # to explain it. None (the default) leaves the chart's own default in
    # place; an explicit {} OVERRIDES it with "schedule anywhere", which
    # is a different statement.
    node_selector: Optional[Dict[str, str]] = None
    tolerations: Optional[List[Dict[str, Any]]] = None

    @property
    def endpoint(self) -> str: ...      # real http://... URL, no credential
```

Rules when generating code:
- `kubeconfig_path` is required and has no ambient fallback — the SDK
  deliberately never reads `$KUBECONFIG`/`~/.kube/config`.
- Tokenizer depends on a cluster only — nothing else needs to exist
  first.
- It holds no credential and needs no Secret — it counts tokens and
  nothing else, reachable only inside the cluster.
- Once deployed, this Tokenizer's `endpoint` is what a `type="tpm"`
- `node_selector`/`tolerations` pin this service to a particular node. They matter here because there is no in-cluster registry: a service image exists only on the node it was imported to, so a pod scheduled anywhere else sits in `ImagePullBackOff` with nothing in the release to explain it. Leave both unset unless the user tells you which node holds the image — a node name or label is a real fact about their cluster, never one to invent. Note `None` and `{}` are different statements: unset leaves the chart's own default in place, while an explicit `{}` overrides it with "schedule anywhere".
  Policy's `TPMOptions.tokenizer_url` should be set to (see the `policy`
  skill) — that wiring is manual, not automatic, since `tokenizer_url`
  lives on Policy's options object rather than on Policy itself, and
  only the tpm driver reads it. If a whole-stack request composes both
  `tokenizer` and a `type="tpm"` policy together, see the `full_stack`
  skill/module docstring for whether that specific case auto-wires.
