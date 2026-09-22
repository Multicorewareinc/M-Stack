---
name: inference
description: Use this skill when the user's request mentions serving a model, vLLM, an inference deployment, GPU/CPU model serving, or what a model gateway should proxy to. Covers the real MultiStack SDK Inference/VLLMOptions/InferenceBackend classes.
---

# MultiStack SDK — Inference / VLLMOptions / InferenceBackend

Real class signatures (from `multistack.inference`, re-exported at the top
level as `from multistack import Inference, InferenceBackend, DEFAULT_MODEL`
— note `VLLMOptions` itself is only at the `multistack.inference` submodule
level, not top-level). Inference installs *into an existing cluster* — it
does not create one. "vllm" is the only supported implementation today.
This is what a Gateway's `upstream_url` points at (see the `gateway`
skill) — deploy this first, then wire a Gateway to its `endpoint`.

`Inference`/`VLLMOptions` are Pydantic models, and `build_inference_plan`'s
`inference` parameter is typed as `Inference` directly — the field-by-field
shape below is generated straight from these classes for the tool schema
a model actually receives, not retyped by hand.

```python
class VLLMOptions(BaseModel):
    s3_mirror_image: str = "quay.io/minio/mc:RELEASE.2025-07-16T15-35-03Z"
    ready_timeout: int = 1800    # generous -- a cold CPU deploy pulls ~2GB and compiles the model, minutes not seconds
    warmup_timeout: int = 600

# `type` selects the options class. LlamaCppOptions is the second
# implementation; its fields are not documented here yet, so prefer
# type="vllm" unless the user explicitly asks for llama.cpp.
class LlamaCppOptions(BaseModel): ...

class Inference(BaseModel):
    type: str = "vllm"                # or "llamacpp"
    kubeconfig_path: str              # required -- the EXISTING cluster this installs onto
    namespace: Optional[str] = None
    options: Optional[VLLMOptions] = None   # auto-filled from `type` if left unset

    model: str = "Qwen/Qwen2.5-0.5B-Instruct"   # the only model this deployment is sized/tested for by default -- override freely, it's a normal field
    name: str = "vllm"
    replicas: int = 1
    port: int = 8000

    device: str = "cpu"               # or "gpu"
    dtype: Optional[str] = None       # None = pick from the node's CPU flags; "auto"/"float32"/"float16"/"bfloat16"
    max_model_len: int = 4096
    gpu_count: int = 0                # only meaningful with device="gpu" -- never set on a cpu device

    cpu_cores: int = 6
    memory_gb: int = 12               # must exceed kv_cache_gb + shm_gb -- both live inside this limit
    kv_cache_gb: int = 4
    shm_gb: int = 2                   # vLLM's shm queue needs a few hundred MiB minimum; k8s defaults to 64Mi

    node_selector: dict = {}
    tolerations: list = []
    host_network: bool = False        # off by default -- the gateway consumes this in-cluster, where a plain Service is right

    # Serve weights already in an object store instead of pulling from a
    # model registry: set `model` to "s3://bucket/path" AND both of these
    # -- all three or none, the SDK rejects a partial combination.
    s3_endpoint_url: Optional[str] = None
    s3_secret_name: Optional[str] = None   # the Secret holding AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY -- a NAME, never real credentials
    s3_insecure_tls: bool = False

    served_model_name: Optional[str] = None   # the id clients pass in the OpenAI API -- defaults to the last path segment of an s3:// model

    # matchLabels selectors allowed to reach this service on `port`
    # (e.g. [{"app": "model-gateway"}]). Empty => NO NetworkPolicy is
    # rendered at all, so an existing deployment is untouched until a
    # caller opts in.
    allowed_client_labels: List[Dict[str, str]] = []
    # Labels for a ServiceMonitor scraping vLLM's own /metrics. Empty =>
    # none is rendered, same opt-in shape as allowed_client_labels above.
    service_monitor_labels: Dict[str, str] = {}
    warmup: bool = True               # sends one request after readiness so the first REAL user doesn't pay the cold-start cost
    runtime_class: Optional[str] = None   # e.g. "nvidia" -- needed for GPU serving on older RKE2/GPU Operator combos
    image: Optional[str] = None
    extra_args: list = []             # appended verbatim to `vllm serve`, for flags this spec doesn't model

    def validate(self) -> None: ...   # raises ValueError on:
        # - device not "cpu"/"gpu"
        # - gpu_count >= 1 required when device="gpu"; gpu_count set while device="cpu"
        # - memory_gb <= kv_cache_gb + shm_gb
        # - replicas > 1 combined with host_network=True (every replica would contend for one host port)
        # - an s3:// model missing s3_endpoint_url or s3_secret_name, or a non-s3 model with s3_endpoint_url set

class InferenceBackend:
    def create(self, inference: Inference, nodes: Optional[list] = None) -> str: ...   # returns the endpoint; nodes is optional, only used to check prerequisites/pick a cpu dtype
    def delete(self, inference: Inference) -> None: ...
    def check_prerequisites(self, inference: Inference, nodes=None) -> list: ...
    def endpoint(self, inference: Inference) -> str: ...
```

Rules when generating code:
- `kubeconfig_path` is required and has no ambient fallback — the SDK deliberately never reads `$KUBECONFIG`/`~/.kube/config`, so a stale default can't silently target the wrong cluster.
- `build_inference_plan` is for adding a model server to a cluster that **already exists**. If the cluster is also being created in this same request, use `build_full_stack_plan` instead.
- Inference depends on a cluster (`REQUIRES = ("cluster",)`) only — it does NOT require Storage/MinIO to exist, unless the user specifically wants weights served from an `s3://` object-store path.
- `model` defaults to a small CPU-sized model (`Qwen/Qwen2.5-0.5B-Instruct`) if the user doesn't specify one — that default is only right for casual/testing use; if the user describes any real capability or size need, ask which model rather than silently using the default.
- Never set `gpu_count` unless `device="gpu"`, and never invent a `gpu_count` the user didn't give — ask if they want GPU serving but haven't said how many.
- The three S3 fields (`model` as `s3://...`, `s3_endpoint_url`, `s3_secret_name`) travel together — set all three or none. `s3_secret_name` is a Secret **name**, never a real access key.
- `nodes` is optional (unlike `build_storage_plan`'s, which is required) — pass it when the user has given real node details and wants prerequisite checking; it's fine to omit if they haven't.
- Once deployed, this Inference's `endpoint` is what a Gateway's `upstream_url` should point at (see the `gateway` skill / `build_gateway_plan`) — deploying inference alone serves nothing to end users until a Gateway (or another client) actually calls it.
- `allowed_client_labels` locks down who may reach this model server on its port, and it is **off by default**: an empty list renders no NetworkPolicy at all. Only set it when the user asks to restrict access, with real selector labels they gave you (typically the Model Gateway's, e.g. `[{"app": "model-gateway"}]`) — a wrong selector leaves the model deployed and healthy but unreachable by its own gateway.
- `service_monitor_labels` turns on Prometheus scraping of vLLM's own `/metrics`, and is likewise **off by default**: an empty dict renders no ServiceMonitor. Set it only when an Observability capability is already installed, to whatever label its Prometheus actually selects on — for kube-prometheus-stack that is `{"release": "<its release name>"}`. Never guess the value: a wrong label applies cleanly and reports no error, but the target is silently dropped and no metrics ever arrive.
