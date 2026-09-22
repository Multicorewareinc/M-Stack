"""
The inference capability: an OpenAI-compatible model server on a cluster.

    Inference(type="vllm", kubeconfig_path=kc, model="Qwen/Qwen2.5-0.5B-Instruct")

`vllm` is the only implementation today. `model` stays a normal, caller-set
field — defaulted to Qwen2.5-0.5B-Instruct for now because that is the only
model this deployment has been sized and tested for, not because the field
is locked. A later caller (the agentic layer, an API request) picks the
model simply by passing a different value here; nothing about the shape
changes.
"""
from __future__ import annotations
from typing import Any, ClassVar, Dict, List, Optional, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..capability import CapabilitySpec

SUPPORTED_TYPES: Tuple[str, ...] = ("vllm", "llamacpp")

# The only model this deployment is sized/tested for today. `model` remains
# a normal field so a caller can override it once more are supported.
DEFAULT_MODEL = "Qwen/Qwen2.5-0.5B-Instruct"

DEFAULT_CPU_IMAGE = "vllm/vllm-openai-cpu:v0.28.0-x86_64"
DEFAULT_GPU_IMAGE = "vllm/vllm-openai:v0.28.0"

# The CUDA server build, pinned by digest rather than tag. This is the
# image the platform's GPU deployment actually runs, and llama.cpp
# publishes rolling tags -- a tag would change what "the same
# deployment" means between two installs of the same spec.
DEFAULT_LLAMACPP_IMAGE = (
    "ghcr.io/ggml-org/llama.cpp"
    "@sha256:93e3b8da7ce2d7c501e826e3c87b6fde980fac18a8058d38e24529a5b3b83b94"
)

# Where the init container mirrors s3:// weights to, and the image that
# does the mirroring — `mc`, the MinIO Client CLI (works against any
# S3-compatible store, not just MinIO). vLLM's own `runai_model_streamer`
# loader isn't in the official CPU image, so weights land on local disk
# first.
LOCAL_MODEL_DIR = "/models"

# quay.io, not Docker Hub: docker.io/minio/mc stopped serving anonymous
# pulls (401 insufficient_scope on every tag, current and old), which made
# the previous `minio/mc:latest` default unpullable. quay.io is also where
# the MinIO operator chart points its own images, so the two now agree.
#
# Pinned, and deliberately two releases behind the newest — "latest" is
# what turned a registry change into a deployment failure with no diff to
# look at, and the newest release is the one nobody has run yet. Verified
# by pulling it on rke2-wrk-2:
#
#   mc version RELEASE.2025-07-16T15-35-03Z  go1.24.4 linux/amd64
#   quay.io/minio/mc@sha256:d5bccfe71e95a34b25d626d86621930342553657e8776833b65ae0bc63cf4928
#
# Overridable per spec via VLLMOptions.s3_mirror_image.
DEFAULT_MIRROR_IMAGE = "quay.io/minio/mc:RELEASE.2025-07-16T15-35-03Z"

# CPU feature flags that make a dtype natively fast rather than emulated.
DTYPE_CPU_FLAGS = {
    "bfloat16": ("avx512_bf16", "amx_bf16"),
    "float16": ("avx512_fp16",),
}

SUPPORTED_DEVICES: Tuple[str, ...] = ("cpu", "gpu")
SUPPORTED_DTYPES: Tuple[str, ...] = ("auto", "float32", "float16", "bfloat16")


class VLLMOptions(BaseModel):
    """Deployment mechanics for the vllm implementation — not part of the
    model request shape, which lives on `Inference` itself."""

    model_config = ConfigDict(extra="forbid")

    s3_mirror_image: str = DEFAULT_MIRROR_IMAGE
    # Generous: a cold CPU deployment pulls a ~2GB image, downloads
    # weights, and compiles the model — minutes, not seconds.
    ready_timeout: int = 1800
    warmup_timeout: int = 600

    def validate(self) -> None:
        return None


class LlamaCppOptions(BaseModel):
    """Deployment mechanics for the llamacpp implementation.

    Why this implementation exists alongside vllm: llama.cpp runs
    quantised GGUF weights, so a 7B model fits a consumer card that vLLM
    cannot use at all. The platform's GPU is a compute capability 6.1
    card, below the 7.0 floor of every PyTorch build vLLM ships -- so on
    this cluster llamacpp is not an alternative to vllm, it is the only
    way to use the GPU, and it is what the agentic layer tests against.
    """

    model_config = ConfigDict(extra="forbid")

    image: str = DEFAULT_LLAMACPP_IMAGE

    # `-ngl`. 99 means "offload every layer there is" -- llama.cpp takes a
    # layer count, not a fraction, and any number past the model's depth
    # is clamped. 0 keeps the model on the CPU.
    n_gpu_layers: int = 99

    # `--cache-type-k`. Quantising the K cache is what makes a large
    # context fit alongside the weights; q8_0 costs little accuracy. None
    # leaves llama.cpp's own f16 default.
    cache_type_k: Optional[str] = "q8_0"

    # `-ub` / `-b`. Lowered from llama.cpp's defaults because the KV cache
    # for a 64k context leaves little headroom on an 11GB card, and the
    # batch buffers are allocated up front.
    ubatch_size: int = 256
    batch_size: int = 512

    # Where the GGUF lands on the node. A hostPath, not a PVC, and
    # deliberately: `-hf` downloads ~5GB per model on first start, the
    # deployment is pinned to one node anyway, and this survives a
    # cluster teardown -- `rke2-uninstall.sh` removes rke2's own
    # directories and leaves this one, which is the difference between a
    # rebuild that re-downloads 13GB and one that does not.
    model_cache_path: str = "/var/lib/llamacpp-models"

    # RKE2 ships the `nvidia` RuntimeClass via rke2-runtimeclasses, and
    # the container needs it to see the card at all. None omits the field,
    # for a cluster whose default runtime already has the GPU hooks.
    runtime_class_name: Optional[str] = "nvidia"

    # NodePort, matching what runs: the agentic layer reaches this from
    # outside the cluster and there is no Route in front of it. ClusterIP
    # is the better answer once one exists.
    service_type: str = "NodePort"

    # Pin it, or Kubernetes picks a free one on every create. That matters
    # here more than it usually does: the address is *published* -- see
    # `LLM_BASE_URL` in agentic/backend/.env.example, which names a port -- so an
    # allocation that drifts on a rebuild leaves every copied .env
    # pointing at nothing, with no error on this side. None keeps
    # Kubernetes' own behaviour, for a caller reaching the Service by
    # name.
    node_port: Optional[int] = None

    # A cold start downloads the weights, then loads them onto the card.
    ready_timeout: int = 1800

    def validate(self) -> None:
        if self.service_type not in ("ClusterIP", "NodePort", "LoadBalancer"):
            raise ValueError(
                f"service_type={self.service_type!r} is not a Service type"
            )
        if self.node_port is not None and self.service_type == "ClusterIP":
            raise ValueError(
                "node_port is set but service_type is 'ClusterIP', which has "
                "no node ports -- the API server would reject the Service."
            )


class Inference(CapabilitySpec):
    """One inference deployment serving an OpenAI-compatible API."""

    CAPABILITY: ClassVar[str] = "inference"
    SUPPORTED_TYPES: ClassVar[Tuple[str, ...]] = SUPPORTED_TYPES
    OPTIONS_FOR_TYPE: ClassVar[Dict[str, type]] = {
        "vllm": VLLMOptions, "llamacpp": LlamaCppOptions,
    }
    DEFAULT_NAMESPACES: ClassVar[Dict[str, str]] = {
        "vllm": "inference", "llamacpp": "inference",
    }
    REQUIRES: ClassVar[Tuple[str, ...]] = ("cluster",)

    FROM_STACK: ClassVar[Dict[str, str]] = {
        "kubeconfig_path": "kubeconfig_path",
        # Pulled from a storage capability elsewhere in the stack, if one
        # provisioned the bucket/credentials this deployment mirrors from.
        "s3_endpoint_url": "s3_endpoint_url",
        "s3_secret_name": "s3_secret_name",
    }
    # The in-cluster endpoint a gateway's upstream_url can point at.
    PROVIDES: ClassVar[Dict[str, str]] = {"inference_endpoint": "endpoint"}

    type: str = "vllm"
    kubeconfig_path: str
    namespace: Optional[str] = None
    # A Union, so the type discriminator in CapabilitySpec resolves a
    # dict to the right model -- see its own note on why best-fit
    # resolution silently produced the wrong one for Policy.
    options: Optional[Union[VLLMOptions, LlamaCppOptions]] = None

    # The model to serve. Left open for the caller — see module docstring.
    model: str = DEFAULT_MODEL

    name: str = "vllm"
    replicas: int = 1
    port: int = 8000

    device: str = "cpu"
    # None means "decide from the target node's CPU flags" — see
    # VLLMDriver.recommended_dtype. Otherwise vLLM takes the dtype from the
    # model config, which is how a bfloat16 model gets emulated on a CPU
    # with no bf16 support.
    dtype: Optional[str] = None
    max_model_len: int = 4096
    gpu_count: int = 0

    # requests == limits (Guaranteed QoS): a cpu limit below the OpenMP
    # thread count makes cgroup throttling fight the thread pool.
    cpu_cores: int = 6
    memory_gb: int = 12
    kv_cache_gb: int = 4  # VLLM_CPU_KVCACHE_SPACE, counted in memory_gb.
    # k8s defaults /dev/shm to 64Mi; vLLM's shm queue needs ~160Mi minimum.
    shm_gb: int = 2

    node_selector: Dict[str, str] = Field(default_factory=dict)
    tolerations: List[Dict[str, Any]] = Field(default_factory=list)

    # Serves on the node's own address instead of a pod IP — a workaround
    # for reaching the pod from outside the cluster. Off by default: the
    # gateway consumes this in-cluster, where the plain Service is right.
    host_network: bool = False

    # Points at an existing S3-compatible bucket holding the model weights —
    # this capability only mirrors from it, it never creates the bucket.
    s3_endpoint_url: Optional[str] = None
    # Name of an existing Secret with AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY
    # keys; mounted into the mirror init container's env (see driver).
    s3_secret_name: Optional[str] = None
    # Skip TLS verification against s3_endpoint_url — for self-signed/dev
    # object stores, not for production endpoints.
    s3_insecure_tls: bool = False

    served_model_name: Optional[str] = None

    # matchLabels selectors (e.g. [{"app": "model-gateway"}]) allowed to
    # reach this service on `port`. Empty => no NetworkPolicy is rendered
    # at all, so an existing deployment is untouched until a caller opts
    # in (ADR-006: features attach by config, not code).
    allowed_client_labels: List[Dict[str, str]] = Field(default_factory=list)

    # Labels for a `ServiceMonitor` (the Prometheus Operator CRD
    # kube-prometheus-stack's Prometheus reads) scraping vLLM's own
    # `/metrics` — enabled in the server by default, nothing to turn on
    # container-side. Empty => no ServiceMonitor is rendered at all, same
    # ADR-006 shape as `allowed_client_labels`. Set this to whatever label
    # the installed Observability's Prometheus actually selects on — e.g.
    # `{"release": observability.options.release_name}` for
    # kube-prometheus-stack's own default selector — see
    # `multistack/servicemonitor/README.md`; a guessed value is worse than
    # none.
    service_monitor_labels: Dict[str, str] = Field(default_factory=dict)

    # First request after readiness is ~an order of magnitude slower than
    # steady state; this absorbs that cost at deploy time instead.
    warmup: bool = True

    runtime_class: Optional[str] = None
    image: Optional[str] = None
    extra_args: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_on_construction(self):
        """CapabilitySpec's own hook only runs `validate_capability()`; this
        also runs the checks below, at construction and on every
        assignment, so a spec can never exist in a state it would later
        reject."""
        self.validate()
        return self

    def validate(self) -> None:
        self.validate_capability()
        if not self.kubeconfig_path:
            raise ValueError(
                "kubeconfig_path is required — this deploys into an existing "
                "cluster, and the SDK won't fall back to ambient "
                "$KUBECONFIG/~/.kube/config, which can silently target the "
                "wrong cluster."
            )
        if not self.model:
            raise ValueError(f"model is required (e.g. '{DEFAULT_MODEL}')")

        if self.device not in SUPPORTED_DEVICES:
            raise ValueError(
                f"Invalid device '{self.device}'. Expected one of {SUPPORTED_DEVICES}."
            )
        if self.dtype is not None and self.dtype not in SUPPORTED_DTYPES:
            raise ValueError(
                f"Invalid dtype '{self.dtype}'. Expected one of {SUPPORTED_DTYPES} "
                "or None to select from the target node's CPU flags."
            )
        if self.device == "gpu" and self.gpu_count < 1:
            raise ValueError("device='gpu' needs gpu_count >= 1")
        if self.device == "cpu" and self.gpu_count:
            raise ValueError(
                f"gpu_count={self.gpu_count} is set but device is 'cpu' — pass "
                "device='gpu' to request accelerators."
            )

        if self.cpu_cores < 1:
            raise ValueError(f"cpu_cores must be at least 1, got {self.cpu_cores}")
        if self.replicas < 1:
            raise ValueError(f"replicas must be at least 1, got {self.replicas}")
        if self.shm_gb < 1:
            raise ValueError(
                f"shm_gb={self.shm_gb} is too small — vLLM's shm message queue "
                "needs a few hundred MiB and Kubernetes only gives 64Mi by "
                "default, which fails at startup."
            )
        if self.memory_gb <= self.kv_cache_gb + self.shm_gb:
            raise ValueError(
                f"memory_gb={self.memory_gb} must exceed kv_cache_gb"
                f"={self.kv_cache_gb} plus shm_gb={self.shm_gb}, which are both "
                "allocated inside the container's memory limit — leaving nothing "
                "for the model weights."
            )
        if self.replicas > 1 and self.host_network:
            raise ValueError(
                f"replicas={self.replicas} with host_network=True would make "
                f"every replica contend for host port {self.port}. Use one "
                "replica per node, or turn host_network off."
            )
        if self.s3_endpoint_url and not self.model_is_s3:
            raise ValueError(
                "s3_endpoint_url is set but model isn't an s3:// path — point "
                "`model` at the object store (e.g. 's3://models/Qwen2.5-0.5B')."
            )
        if self.model_is_s3 and not self.s3_endpoint_url:
            raise ValueError(
                f"model '{self.model}' is an s3:// path but s3_endpoint_url is "
                "unset, so there is no store to fetch it from."
            )
        if self.model_is_s3 and not self.s3_secret_name:
            raise ValueError(
                "s3_secret_name is required for an s3:// model — the mirror "
                "needs credentials from a Secret holding "
                "AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY."
            )

    @property
    def model_is_s3(self) -> bool:
        """True when `model` names an s3:// object rather than a HuggingFace
        repo id — switches the driver to mirror weights locally first
        instead of letting vLLM pull from the HF cache."""
        return self.model.startswith("s3://")

    @property
    def served_name(self) -> str:
        """The model id clients pass to the OpenAI API."""
        if self.served_model_name:
            return self.served_model_name
        return self.model.rstrip("/").rsplit("/", 1)[-1]

    @property
    def model_path(self) -> str:
        if self.model_is_s3:
            return f"{LOCAL_MODEL_DIR}/{self.served_name}"
        return self.model

    @property
    def container_image(self) -> str:
        if self.image:
            return self.image
        return DEFAULT_GPU_IMAGE if self.device == "gpu" else DEFAULT_CPU_IMAGE

    @property
    def service_name(self) -> str:
        return self.name

    @property
    def endpoint(self) -> str:
        """The in-cluster base URL — what a gateway's upstream_url points
        at. With host_network, the pod's real (node-IP) address is only
        knowable from the cluster, so the driver's own `endpoint()` method
        is the source of truth in that case; this stays the static form."""
        return (f"http://{self.name}.{self.resolved_namespace}"
                f".svc.cluster.local:{self.port}")

    def container_args(self, dtype: Optional[str] = None) -> List[str]:
        args = [
            "--model", self.model_path,
            "--max-model-len", str(self.max_model_len),
            "--host", "0.0.0.0",
            "--port", str(self.port),
        ]
        if self.model_path != self.model:
            args += ["--served-model-name", self.served_name]
        resolved = dtype or self.dtype
        if resolved:
            args += ["--dtype", resolved]
        if self.device == "gpu" and self.gpu_count > 1:
            args += ["--tensor-parallel-size", str(self.gpu_count)]
        return args + list(self.extra_args)

    def env(self) -> Dict[str, str]:
        env: Dict[str, str] = {}
        if self.device == "cpu":
            env["VLLM_CPU_KVCACHE_SPACE"] = str(self.kv_cache_gb)
            env["OMP_NUM_THREADS"] = str(self.cpu_cores)
            env["VLLM_CPU_OMP_THREADS_BIND"] = f"0-{self.cpu_cores - 1}"
        return env

    def mirror_command(self) -> List[str]:
        """Shell command for the `fetch-weights` init container: registers
        s3_endpoint_url as an `mc` alias using AWS_ACCESS_KEY_ID/
        AWS_SECRET_ACCESS_KEY from the env (sourced from s3_secret_name by
        the driver), then mirrors the existing remote object/prefix down to
        model_path. Read-only against the bucket — nothing is created or
        uploaded."""
        insecure = " --insecure" if self.s3_insecure_tls else ""
        remote = self.model[len("s3://"):]
        return ["sh", "-c", (
            f'set -e\n'
            f'mc alias set src "{self.s3_endpoint_url}" '
            f'"$AWS_ACCESS_KEY_ID" "$AWS_SECRET_ACCESS_KEY"{insecure}\n'
            f'mc mirror --overwrite{insecure} "src/{remote}" "{self.model_path}"\n'
            f'ls -l "{self.model_path}"'
        )]
