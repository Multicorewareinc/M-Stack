---
name: inference
description: Use this skill when the user's request mentions serving a model, vLLM, llama.cpp, llamacpp, GGUF, KServe, an InferenceService, scale-to-zero, canary traffic splitting, an inference deployment, GPU/CPU model serving, or what a model gateway should proxy to. Covers the real MultiStack SDK Inference/VLLMOptions/LlamaCppOptions/KServeOptions/InferenceBackend classes.
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
    s3_mirror_image: str = "amazon/aws-cli:2.37.4@sha256:fdd8d1fcbea9c371678dee5a40df8b178c7a781b4586605756ee28114c97ead6"
    ready_timeout: int = 1800    # generous -- a cold CPU deploy pulls ~2GB and compiles the model, minutes not seconds
    warmup_timeout: int = 600

# `type` selects the options class. type="llamacpp" runs quantised GGUF
# weights, so a 7B model fits a consumer card, and it can use a GPU below
# compute capability 7.0, which no vLLM build supports. Use it when the
# user asks for llama.cpp or for GPU serving on such a card; otherwise
# type="vllm".
class LlamaCppOptions(BaseModel):
    image: str = "ghcr.io/ggml-org/llama.cpp@sha256:..."   # pinned by digest; leave the default
    n_gpu_layers: int = 99              # layers offloaded to the GPU; 99 = all of them, 0 = CPU only
    cache_type_k: Optional[str] = "q8_0"   # quantised K cache so a long context fits; None = llama.cpp's f16
    ubatch_size: int = 256              # -ub; lowered so a 64k-context KV cache fits an 11GB card
    batch_size: int = 512               # -b
    model_cache_path: str = "/var/lib/llamacpp-models"   # hostPath on the node where downloaded GGUF files are kept across restarts and rebuilds
    runtime_class_name: Optional[str] = "nvidia"   # the RuntimeClass the container needs to see the card; None = the cluster's default runtime
    service_type: str = "NodePort"      # "ClusterIP", "NodePort" or "LoadBalancer"
    node_port: Optional[int] = None     # pin it so the published address survives a rebuild; not allowed with ClusterIP
    ready_timeout: int = 1800

class KServePlatform(BaseModel):
    """KServe itself, installed by this driver when missing -- unlike the
    vllm/llamacpp drivers, which never install anything beyond the model
    server they render."""
    install: bool = True              # False refuses on a missing platform instead of installing it
    namespace: str = "kserve"
    chart_version: Optional[str] = "v0.20.0"
    crd_chart: str = "oci://ghcr.io/kserve/charts/kserve-crd"
    controller_chart: str = "oci://ghcr.io/kserve/charts/kserve-resources"
    install_runtime_configs: bool = True   # the built-in ClusterServingRuntimes -- off only if the cluster already has its own
    runtime_configs_chart: str = "oci://ghcr.io/kserve/charts/kserve-runtime-configs"
    cert_manager: bool = True         # KServe's webhooks need TLS cert-manager issues -- off only if the cluster already runs one
    cert_manager_namespace: str = "cert-manager"
    cert_manager_chart_version: Optional[str] = None
    knative_operator_namespace: str = "knative-operator"
    knative_serving_namespace: str = "knative-serving"
    knative_chart_version: Optional[str] = None
    knative_ingress: str = "kourier"  # or "istio" -- only matters for deployment_mode="Serverless"
    knative_ingress_service_type: Optional[str] = None   # "LoadBalancer"/"NodePort"/"ClusterIP"; None leaves Knative's own default
    install_timeout: int = 900        # generous -- three or four chart installs, each pulling images

class KServeOptions(BaseModel):
    """Deployment mechanics for type="kserve": KServe owns the workload
    (it takes an InferenceService and produces the Deployment/Service/
    autoscaler itself) instead of this SDK rendering one directly, which
    is what buys scale-to-zero, canary traffic splitting and S3/HF
    fetching with no mirror init container. Trade-off: KServe must exist
    on the cluster first -- platform.install=True (the default) makes
    this driver install it rather than requiring someone did already."""
    deployment_mode: str = "RawDeployment"   # or "Serverless"/"Knative" (needs Knative Serving too -- see platform)
    model_format: str = "huggingface"  # selects the ClusterServingRuntime KServe matches against
    runtime: Optional[str] = None      # pins a specific ClusterServingRuntime by name, bypassing model_format
    min_replicas: Optional[int] = None  # 0 = scale-to-zero, needs Serverless (or RawDeployment+KEDA on newer KServe)
    max_replicas: Optional[int] = None
    canary_traffic_percent: Optional[int] = None   # traffic the new revision takes; None deploys replacing outright
    openai_path_prefix: str = "/openai"   # where the huggingface runtime mounts the OpenAI routes -- NOT "/v1" like plain vLLM
    predictor_service_port: int = 80   # KServe's own fixed port for its generated Service -- not Inference.port
    metrics_port_name: Optional[str] = None   # ServiceMonitor port-name match; None derives it from KServe's own naming
    predictor_container_port: int = 8080   # what the predictor container actually listens on -- not Inference.port
    service_account_name: Optional[str] = None   # attached to the predictor pod so KServe's storage-initializer can read a private bucket
    platform: KServePlatform = KServePlatform()
    ready_timeout: int = 1800
    warmup_timeout: int = 600

class Inference(BaseModel):
    type: str = "vllm"                # or "llamacpp" or "kserve"
    kubeconfig_path: str              # required -- the EXISTING cluster this installs onto
    namespace: Optional[str] = None
    options: Optional[Union[VLLMOptions, LlamaCppOptions, KServeOptions]] = None   # auto-filled from `type` if left unset

    model: str = "Qwen/Qwen2.5-0.5B-Instruct"   # the only model this deployment is sized/tested for by default -- override freely, it's a normal field
    name: str = "vllm"
    replicas: int = 1
    port: int = 8000

    device: str = "cpu"               # or "gpu"
    dtype: Optional[str] = None       # None = pick from the node's CPU flags; "auto"/"float32"/"float16"/"bfloat16"
    max_model_len: int = 4096
    gpu_count: int = 0                # only meaningful with device="gpu" -- never set on a cpu device
    gpu_resource: str = "nvidia.com/gpu"   # the extended resource a GPU is requested as -- AMD's ROCm plugin advertises "amd.com/gpu" instead; never invent a value other than what the cluster's device plugin actually advertises

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
- Inference depends on a cluster (`REQUIRES = ("cluster",)`) only — it does NOT require Storage/SeaweedFS to exist, unless the user specifically wants weights served from an `s3://` object-store path.
- `model` defaults to a small CPU-sized model (`Qwen/Qwen2.5-0.5B-Instruct`) if the user doesn't specify one — that default is only right for casual/testing use; if the user describes any real capability or size need, ask which model rather than silently using the default.
- Never set `gpu_count` unless `device="gpu"`, and never invent a `gpu_count` the user didn't give — ask if they want GPU serving but haven't said how many.
- `gpu_resource` only matters on `device="gpu"`, and only needs changing from its NVIDIA default when the user says the cluster uses AMD (ROCm advertises `amd.com/gpu`) or otherwise names a non-NVIDIA resource — never guess a vendor the user hasn't stated.
- **`type="kserve"` is for scale-to-zero, canary traffic splitting, or S3/HF weight fetching with no mirror container** — ask for it only when the user wants one of those, not as a default over vllm/llamacpp. Unlike those two, this driver does not render its own Deployment: it hands KServe an `InferenceService` and KServe produces everything else. `KServePlatform.install` defaults to `True`, so this driver installs KServe itself (cert-manager, CRDs, the controller) if it isn't already on the cluster — never tell the user KServe must be pre-installed unless they've explicitly set `install=False`.
- `deployment_mode="RawDeployment"` (the default) needs nothing but KServe itself. `"Serverless"` additionally needs Knative Serving — on a cluster without it, the InferenceService is silently accepted and never becomes ready, with no error on the object itself. **`min_replicas=0` (scale-to-zero) alone is not a reason to switch**: RawDeployment supports it too on newer KServe (with KEDA), and whether a given cluster's KServe version does is exactly what this spec can't tell offline — leave `deployment_mode` at its default and let `check_prerequisites` warn if it can't confirm scale-to-zero support. Only switch to Serverless when the user names something RawDeployment can't do at all: canary traffic splitting.
- `KServeOptions.predictor_service_port` and `.predictor_container_port` are KServe's own fixed ports for what it generates — **never** write `Inference.port` for a kserve deployment; nothing forwards it, and a NetworkPolicy written against it would open a port the pod never serves.
- `KServeOptions.openai_path_prefix` defaults to `/openai`, not `/v1` like plain vLLM — a Gateway's `upstream_url` pointed at a kserve deployment needs the prefix included, or the chat endpoint is at the wrong path.
- The three S3 fields (`model` as `s3://...`, `s3_endpoint_url`, `s3_secret_name`) travel together — set all three or none. `s3_secret_name` is a Secret **name**, never a real access key.
- `nodes` is optional (unlike `build_storage_plan`'s, which is required) — pass it when the user has given real node details and wants prerequisite checking; it's fine to omit if they haven't.
- Once deployed, this Inference's `endpoint` is what a Gateway's `upstream_url` should point at (see the `gateway` skill / `build_gateway_plan`) — deploying inference alone serves nothing to end users until a Gateway (or another client) actually calls it.
- `allowed_client_labels` locks down who may reach this model server on its port, and it is **off by default**: an empty list renders no NetworkPolicy at all. Only set it when the user asks to restrict access, with real selector labels they gave you (typically the Model Gateway's, e.g. `[{"app": "model-gateway"}]`) — a wrong selector leaves the model deployed and healthy but unreachable by its own gateway.
- `service_monitor_labels` turns on Prometheus scraping of vLLM's own `/metrics`, and is likewise **off by default**: an empty dict renders no ServiceMonitor. Set it only when an Observability capability is already installed, to whatever label its Prometheus actually selects on — for kube-prometheus-stack that is `{"release": "<its release name>"}`. Never guess the value: a wrong label applies cleanly and reports no error, but the target is silently dropped and no metrics ever arrive.
