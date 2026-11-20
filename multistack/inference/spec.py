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

import re
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..capability import CapabilitySpec

SUPPORTED_TYPES: tuple[str, ...] = ("vllm", "llamacpp", "kserve", "kserve_llm")

# The two KServe-backed types. Several fields mean the same thing to both
# and nothing to the others, so the spec asks "is this KServe" in one place.
KSERVE_TYPES: tuple[str, ...] = ("kserve", "kserve_llm")

# Where the serving copy of the weights lives -- see `Inference.model_cache`.
#   none    today's behaviour: fetched into the pod, gone with the pod
#   node    a copy on the GPU machine's own disk, reused across restarts
#   stream  read straight from the object store into memory, no disk copy
MODEL_CACHE_MODES: tuple[str, ...] = ("none", "node", "stream")

# `cache_scope`: who may use a KServe node cache. `namespace`, the default,
# is a LocalModelNamespaceCache only this deployment's namespace can use:
# its own download credentials, and no other namespace can delete it.
# `cluster` is a LocalModelCache any namespace's deployment of the same
# model can match -- one download account shared by all, chosen on purpose.
# Neither scope separates the files: KServe keeps one copy per model URI on
# each node, so weights are only as private as their URI is unique.
CACHE_SCOPES: tuple[str, ...] = ("cluster", "namespace")

# Where vllm keeps its node cache on the host when `cache_path` is unset.
# Outside RKE2's own directories, so a cluster rebuild finds it again --
# the same reason llamacpp's cache sits in /var/lib.
DEFAULT_NODE_CACHE_PATH = "/var/lib/multistack/models"

# Written next to a finished download. Its contents are the source URI, so
# a cache filled from a different model version is never mistaken for
# this one -- a new version is a new path, and a new path is a new marker.
CACHE_MARKER = ".multistack-complete"

# A path segment that names a version rather than a model: v1, v2.1, 2026-10-01.
VERSION_SEGMENT = re.compile(r"^(v\d+(\.\d+)*|\d{4}-\d{2}-\d{2})$")

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
# does the mirroring. vLLM's own `runai_model_streamer` loader isn't in
# the official CPU image, so by default weights land on local disk first;
# model_cache="stream" is for an image that does carry it.
LOCAL_MODEL_DIR = "/models"

# Used to be quay.io/minio/mc (the MinIO Client CLI) here, chosen
# specifically because it works against any S3-compatible store, not
# just MinIO. That stopped being pullable at all on 2026-09-25: MinIO
# locked down every one of its own images across both quay.io and
# Docker Hub (401 on every tag tried, current and back to mid-2024), and
# even dl.min.io's direct binary download now 410s -- not a registry-side
# issue to route around, MinIO stopped distributing the client entirely.
#
# The AWS CLI replaces it: still a generic S3 client with no dependency
# on MinIO Inc.'s own distribution. A `python:3.11-slim` + boto3 version
# came first and installed boto3 at deploy time, which put PyPI on the
# critical path of every deploy -- see mirror_command() for why that was
# dropped.
#
# Pinned, and by digest rather than a moving tag -- a floating `:latest`
# is exactly what turned a registry change into a deployment failure with
# no diff to look at last time.
#
# Overridable per spec via VLLMOptions.s3_mirror_image.
DEFAULT_MIRROR_IMAGE = (
    "amazon/aws-cli:2.37.4@sha256:fdd8d1fcbea9c371678dee5a40df8b178c7a781b4586605756ee28114c97ead6"
)

# CPU feature flags that make a dtype natively fast rather than emulated.
DTYPE_CPU_FLAGS = {
    "bfloat16": ("avx512_bf16", "amx_bf16"),
    "float16": ("avx512_fp16",),
}

SUPPORTED_DEVICES: tuple[str, ...] = ("cpu", "gpu")
SUPPORTED_DTYPES: tuple[str, ...] = ("auto", "float32", "float16", "bfloat16")


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
    cache_type_k: str | None = "q8_0"

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
    runtime_class_name: str | None = "nvidia"

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
    node_port: int | None = None

    # A cold start downloads the weights, then loads them onto the card.
    ready_timeout: int = 1800

    def validate(self) -> None:
        if self.service_type not in ("ClusterIP", "NodePort", "LoadBalancer"):
            raise ValueError(f"service_type={self.service_type!r} is not a Service type")
        if self.node_port is not None and self.service_type == "ClusterIP":
            raise ValueError(
                "node_port is set but service_type is 'ClusterIP', which has "
                "no node ports -- the API server would reject the Service."
            )


DEFAULT_KSERVE_VERSION = "v0.20.0"

# KServe renamed both deployment modes in 0.20: "Serverless" became
# "Knative" and "RawDeployment" became "Standard". The old spellings are
# kept in KServe as deprecated aliases and still work, so both are accepted
# here -- a spec written against 0.15 keeps working -- and the canonical
# pair is what the driver emits. DEPLOYMENT_MODE_ALIASES maps old to new;
# anything already canonical maps to itself.
KNATIVE_DEPLOYMENT_MODE = "Knative"
STANDARD_DEPLOYMENT_MODE = "Standard"
DEPLOYMENT_MODE_ALIASES: dict[str, str] = {
    "Serverless": KNATIVE_DEPLOYMENT_MODE,
    "Knative": KNATIVE_DEPLOYMENT_MODE,
    "RawDeployment": STANDARD_DEPLOYMENT_MODE,
    "Standard": STANDARD_DEPLOYMENT_MODE,
}
SUPPORTED_DEPLOYMENT_MODES: tuple[str, ...] = tuple(DEPLOYMENT_MODE_ALIASES)

# Where KServe's own huggingfaceserver mounts the OpenAI-compatible routes.
# A runtime built around vLLM's own server mounts them at the root instead,
# so this is right for the built-in runtime and wrong for most custom ones --
# which is why it is a field, and why the driver warns when a custom runtime
# is paired with this default.
DEFAULT_OPENAI_PATH_PREFIX = "/openai"
KSERVE_CRD_CHART = "oci://ghcr.io/kserve/charts/kserve-crd"
# `charts/kserve` was the controller chart's published name up to v0.16.0
# and was never published again; from v0.17.0 it is `charts/kserve-resources`.
# Pinning the version without this would fail on a name that simply has no
# such tag, which reads as a missing version rather than a moved chart.
KSERVE_CONTROLLER_CHART = "oci://ghcr.io/kserve/charts/kserve-resources"
# KServe 0.20 split the built-in ClusterServingRuntimes out of the
# controller chart into this one: `kserve-resources` shipped thirteen of
# them at v0.15 and ships one at v0.20. Without it a default spec --
# model_format="huggingface" and no explicit `runtime` -- has nothing for
# KServe to match, and the InferenceService never goes ready.
KSERVE_RUNTIME_CONFIGS_CHART = "oci://ghcr.io/kserve/charts/kserve-runtime-configs"

# LLMInferenceService ships as its own pair of charts, not as part of
# `kserve-resources`: the CRDs (which also carry the Gateway API Inference
# Extension's InferencePool CRDs) and the llmisvc controller.
KSERVE_LLMISVC_CRD_CHART = "oci://ghcr.io/kserve/charts/kserve-llmisvc-crd"
KSERVE_LLMISVC_CHART = "oci://ghcr.io/kserve/charts/kserve-llmisvc-resources"
# The local model cache is two charts of its own: its CRDs, and its
# controller and node agent. `kserve-resources`' own `kserve.localmodel`
# values only switch the feature on in inferenceservice-config -- confirmed
# against v0.20.0, where setting them renders no controller and no agent.
KSERVE_LOCALMODEL_CRD_CHART = "oci://ghcr.io/kserve/charts/kserve-localmodel-crd"
KSERVE_LOCALMODEL_CHART = "oci://ghcr.io/kserve/charts/kserve-localmodel-resources"

# Where KServe's node agent keeps copies on each machine. Fixed by the agent
# chart, not chosen here: v0.20.0's DaemonSet mounts the host path `/models`
# whatever `kserve.localmodel.agent.hostPath` says (that value is read
# nowhere), and a node group whose volume points elsewhere makes the agent
# download the same model forever.
LOCALMODEL_AGENT_HOST_PATH = "/models"

# The llmisvc controller watches Gateway API objects and LLMInferenceService
# routes are HTTPRoutes, so the standard Gateway API CRDs must exist first.
# The same release `multistack/route` documents for its own HTTPRoutes.
DEFAULT_GATEWAY_API_VERSION = "v1.2.1"

SUPPORTED_LLM_ROUTERS: tuple[str, ...] = ("none", "gateway")

SUPPORTED_KNATIVE_INGRESS: tuple[str, ...] = ("kourier", "istio")

# How Knative's ingress gateway may be exposed. ExternalName is omitted
# deliberately: it names an address rather than serving one, so it is not
# a way to publish a gateway.
SUPPORTED_INGRESS_SERVICE_TYPES: tuple[str, ...] = (
    "LoadBalancer",
    "NodePort",
    "ClusterIP",
)


def _schema_without_docstring(schema: dict[str, Any]) -> None:
    """Keeps a model's docstring out of its JSON schema.

    pydantic copies a class docstring into the schema's `description`, and
    the agentic layer sends these schemas to the model inside every tool
    definition -- where the KServe models' docstrings, written for people
    reading this file, cost about a thousand tokens of a fixed prompt
    budget. So the docstrings stay here for the code's readers and leave
    the schema; guidance the agent needs belongs in the agentic layer's
    own skill notes.
    """
    schema.pop("description", None)


# The KServe option models' config: the repo's usual `extra="forbid"`, plus
# the above.
_KSERVE_MODEL_CONFIG = ConfigDict(
    extra="forbid",
    json_schema_extra=_schema_without_docstring,
)


class KServeModelCache(BaseModel):
    """KServe's local model cache: which machines keep weights on their
    own disk, where, and how much of it.

    Cluster-scoped and installed once, like the rest of the platform. A
    deployment opts in with `Inference(model_cache="node")`, which makes
    the driver create a `LocalModelCache` for its model; this block is what
    that cache lands on -- a `LocalModelNodeGroup` backed by a local
    PersistentVolume at `path` on every machine in `nodes`.

    Why the machines are named rather than selected by an existing label:
    the KServe node agent runs only where `kserve/localmodel=worker` is
    set, and the group's volume is pinned by a label of its own. The
    installer sets both, so the list here is the single statement of which
    machines hold model copies.
    """

    model_config = _KSERVE_MODEL_CONFIG

    enabled: bool = False

    # The LocalModelNodeGroup's name, which a deployment's
    # `cache_node_group` refers to.
    node_group: str = "gpu-local"

    # Kubernetes node names that keep copies. Each is labelled for the
    # KServe agent and for this group's volume.
    nodes: list[str] = Field(default_factory=list)

    # Where the copies live on each machine -- ideally a dedicated NVMe
    # mount, since model loads read from here at disk speed. It has to be
    # where KServe's agent looks, which its v0.20.0 chart fixes at /models
    # (LOCALMODEL_AGENT_HOST_PATH); the installer refuses anything else
    # rather than build a cache the agent never fills.
    path: str = LOCALMODEL_AGENT_HOST_PATH

    # Disk reserved for models on each machine. A LocalModelCache bigger
    # than what is left is refused by KServe rather than filling the disk.
    size_gb: int = 200

    # Where KServe runs the download Jobs. For an s3:// model with a
    # cluster-scoped cache, the bucket credentials Secret must exist here.
    job_namespace: str = "kserve-localmodel-jobs"

    # The no-provisioner StorageClass the group's local volume claims.
    storage_class: str = "kserve-localmodel"

    @model_validator(mode="after")
    def _check(self) -> KServeModelCache:
        if not self.enabled:
            return self
        if not self.nodes:
            raise ValueError(
                "model_cache.enabled is True but no nodes are listed, so no "
                "machine would keep a copy and every LocalModelCache would "
                "wait forever. Name the GPU nodes in model_cache.nodes."
            )
        if not self.path.startswith("/"):
            raise ValueError(
                f"model_cache.path={self.path!r} must be an absolute path on the host."
            )
        if self.size_gb < 1:
            raise ValueError(f"model_cache.size_gb={self.size_gb} leaves no room for a model.")
        for field in ("node_group", "job_namespace", "storage_class"):
            if not str(getattr(self, field)).strip():
                raise ValueError(f"model_cache.{field} must not be empty")
        return self


class KServePlatform(BaseModel):
    """KServe itself, and what it needs, installed by this driver.

    Every other capability in this SDK installs the thing it manages --
    Longhorn, the MinIO operator, the CloudNativePG operator, the NVIDIA
    device plugin. KServe is no different, so the driver installs it
    rather than demanding someone did it first.

    Nothing here is touched when KServe is already present. A cluster
    whose platform team owns KServe keeps its own release: the installer
    checks for the CRD and returns, rather than running an upgrade
    against a release it did not create.

    Serverless additionally installs the Knative *operator*, which is the
    part Knative publishes as a chart, and then hands it a
    `KnativeServing` resource. Serving's ingress layer is selected there
    rather than installed separately, which is why no Kourier or Istio
    chart appears in this model.
    """

    model_config = _KSERVE_MODEL_CONFIG

    # False refuses on a missing platform instead of installing it --
    # for a cluster where a driver must not create cluster-scoped
    # resources, and the message names what to install by hand.
    install: bool = True

    namespace: str = "kserve"
    chart_version: str | None = DEFAULT_KSERVE_VERSION
    crd_chart: str = KSERVE_CRD_CHART
    controller_chart: str = KSERVE_CONTROLLER_CHART
    runtime_configs_chart: str = KSERVE_RUNTIME_CONFIGS_CHART
    # The built-in runtimes, separate from the controller since 0.20. Off
    # only for a cluster whose runtimes someone else owns -- a spec that
    # names its own `runtime` does not need these, but one that relies on
    # model_format matching does.
    install_runtime_configs: bool = True

    # LLMInferenceService's own charts, installed only for type="kserve_llm".
    llmisvc_crd_chart: str = KSERVE_LLMISVC_CRD_CHART
    llmisvc_chart: str = KSERVE_LLMISVC_CHART
    localmodel_crd_chart: str = KSERVE_LOCALMODEL_CRD_CHART
    # None skips installing the Gateway API CRDs, for a cluster that
    # manages them itself.
    gateway_api_version: str | None = DEFAULT_GATEWAY_API_VERSION

    # The local model cache -- off unless a deployment asks for
    # model_cache="node". See KServeModelCache.
    model_cache: KServeModelCache = Field(default_factory=KServeModelCache)

    # KServe's webhooks are served over TLS that cert-manager issues, so
    # this is not optional in practice -- it is a flag only because a
    # cluster may already run cert-manager for everything else.
    cert_manager: bool = True
    cert_manager_namespace: str = "cert-manager"
    cert_manager_chart_version: str | None = None

    knative_operator_namespace: str = "knative-operator"
    knative_serving_namespace: str = "knative-serving"
    knative_chart_version: str | None = None
    knative_ingress: str = "kourier"

    # How the ingress gateway is exposed. None leaves the operator's own
    # default, which is LoadBalancer -- right on a cluster that has a
    # provider, and a dead end on one that does not: the gateway's
    # Service never gets an address.
    #
    # It belongs here rather than being patched afterwards because the
    # Knative operator owns that Service and reconciles it back. Setting
    # the type on the Service directly looks like it worked and is
    # silently reverted within the minute -- observed on a live cluster.
    #
    # Note this alone does not make Serverless usable without a load
    # balancer: NodePort satisfies Knative (route, ingress and revision
    # all go Ready and the model serves), but KServe's own readiness
    # check waits for an external address that a NodePort never has, so
    # the InferenceService stays Unknown. Also observed live. Use it to
    # control exposure, not to escape needing MetalLB.
    knative_ingress_service_type: str | None = None

    # Generous: three or four chart installs, each pulling images, on a
    # cluster that may be fetching them for the first time.
    install_timeout: int = 900

    @model_validator(mode="after")
    def _check(self) -> KServePlatform:
        if self.knative_ingress_service_type is not None and (
            self.knative_ingress_service_type not in SUPPORTED_INGRESS_SERVICE_TYPES
        ):
            raise ValueError(
                "knative_ingress_service_type="
                f"{self.knative_ingress_service_type!r} is not a Service "
                f"type. Expected one of {SUPPORTED_INGRESS_SERVICE_TYPES}, or "
                "None to leave the operator's own default."
            )
        if self.knative_ingress not in SUPPORTED_KNATIVE_INGRESS:
            raise ValueError(
                f"knative_ingress={self.knative_ingress!r} is not one of "
                f"{SUPPORTED_KNATIVE_INGRESS}. It selects the ingress the "
                "KnativeServing resource enables, not a chart to install."
            )
        for field in (
            "namespace",
            "cert_manager_namespace",
            "knative_operator_namespace",
            "knative_serving_namespace",
        ):
            if not str(getattr(self, field)).strip():
                raise ValueError(f"{field} must not be empty")
        if self.install_timeout < 1:
            raise ValueError(
                f"install_timeout={self.install_timeout} leaves no time for a chart to install."
            )
        return self


class KServeOptions(BaseModel):
    """Deployment mechanics for the kserve implementation.

    Why this implementation exists alongside vllm: the vllm and llamacpp
    drivers each render a Deployment and a Service themselves, so the
    cluster holds exactly what this SDK put there. KServe instead owns
    the workload -- it takes an `InferenceService` and produces the
    Deployment, the Service, the autoscaler and the model download. What
    that buys is the part neither other driver has: scale-to-zero,
    canary traffic splitting between revisions, and a storage layer that
    fetches `s3://` and `hf://` weights without a mirror init container.

    The cost is that KServe must already be installed on the cluster --
    this driver deploys *onto* it and never installs it. See
    `KServeDriver.check_prerequisites`, which is written to say so
    before anything is applied.
    """

    model_config = _KSERVE_MODEL_CONFIG

    # RawDeployment needs nothing but KServe itself; Serverless also
    # needs Knative Serving. Raw is the default because the failure mode
    # of the other one is silent: on a cluster without Knative the
    # InferenceService is accepted and simply never becomes ready, with
    # the reason buried in the KServe controller's log rather than on
    # the object.
    deployment_mode: str = "RawDeployment"

    # `spec.predictor.model.modelFormat.name` -- which ClusterServingRuntime
    # KServe selects. "huggingface" is the vLLM-backed runtime that serves
    # the OpenAI routes this capability's callers expect.
    model_format: str = "huggingface"

    # Pins `spec.predictor.model.runtime` to a ClusterServingRuntime by
    # name, bypassing format-based selection. None lets KServe choose
    # from model_format, which is what you want unless the cluster
    # carries a custom runtime.
    runtime: str | None = None

    # None leaves KServe's own defaults rather than writing a number the
    # caller did not ask for. min_replicas=0 is scale-to-zero, which
    # needs Serverless (or RawDeployment with KEDA on newer KServe) --
    # check_prerequisites warns rather than refusing, because which of
    # those a cluster supports is a version question this spec cannot
    # answer offline.
    min_replicas: int | None = None
    max_replicas: int | None = None

    # `canaryTrafficPercent`: how much traffic the newly-deployed
    # revision takes, with the remainder staying on the last one that
    # went ready. None deploys the normal way, replacing outright.
    canary_traffic_percent: int | None = None

    # Where the huggingface runtime mounts the OpenAI-compatible routes,
    # so `{prefix}/v1/chat/completions` is the chat endpoint. Separate
    # from the plain vLLM server, which serves `/v1` at the root -- a
    # caller pointing a gateway here needs the prefix, and warmup below
    # needs it too.
    openai_path_prefix: str = DEFAULT_OPENAI_PATH_PREFIX

    # The port KServe's generated predictor Service listens on. KServe
    # fixes this at 80 and derives the container port itself, so this is
    # not `Inference.port`: overriding it is for a cluster whose KServe
    # has been configured differently, not a normal knob.
    predictor_service_port: int = 80

    # The *name* of the port on KServe's generated predictor Service that
    # a ServiceMonitor scrapes. Prometheus Operator resolves an endpoint
    # by port name, not number, and a name that does not match is the
    # quiet failure the vllm driver documents: the ServiceMonitor is
    # created, targets are discovered, and every one is relabeled into
    # droppedTargets with no error anywhere.
    #
    # None derives it from the Service KServe generates, which names the
    # port after the Service itself -- `<name>-predictor`, not "http".
    # Confirmed against KServe v0.15.0: an InferenceService `qwen`
    # produced a Service `qwen-predictor` whose only port is named
    # `qwen-predictor`. Set it explicitly only for a KServe configured
    # to name it otherwise.
    metrics_port_name: str | None = None

    # The port the predictor container actually listens on, which is
    # KServe's to decide and not `Inference.port`. KServe's runtimes bind
    # 8080 and its generated Service maps 80 -> 8080; nothing forwards
    # `Inference.port` into the InferenceService, so a NetworkPolicy
    # written against that field would open a port the pod never serves
    # and deny every real client. This supersedes `Inference.port` for
    # this implementation.
    predictor_container_port: int = 8080

    # Attached to the predictor pod, so KServe's storage-initializer can
    # read a private bucket. The ServiceAccount is created by this driver
    # and bound to `Inference.s3_secret_name`; the Secret itself is not,
    # and needs KServe's own `serving.kserve.io/s3-*` annotations on it
    # -- check_prerequisites looks for those, because a Secret without
    # them fails inside an init container minutes later.
    service_account_name: str | None = None

    # The platform this deploys onto, and whether the driver installs it.
    platform: KServePlatform = Field(default_factory=KServePlatform)

    # A cold start pulls the runtime image, downloads the weights and
    # loads the model -- the same order of magnitude as the vllm driver's.
    ready_timeout: int = 1800
    warmup_timeout: int = 600

    @property
    def canonical_deployment_mode(self) -> str:
        """`deployment_mode` in the spelling KServe 0.20 uses.

        The old names still work in KServe, but writing the canonical one
        into the InferenceService is what keeps this driver off a
        deprecation path it cannot see the end of.
        """
        return DEPLOYMENT_MODE_ALIASES[self.deployment_mode]

    @property
    def uses_knative(self) -> bool:
        """Whether this mode routes through Knative, under either name.

        A predicate rather than a string compare at each call site: the
        same question was asked in six places, and a rename like 0.20's
        would otherwise have to be caught in all six.
        """
        return self.canonical_deployment_mode == KNATIVE_DEPLOYMENT_MODE

    def validate(self) -> None:
        if self.deployment_mode not in DEPLOYMENT_MODE_ALIASES:
            raise ValueError(
                f"deployment_mode={self.deployment_mode!r} is not a KServe "
                f"deployment mode. Expected one of "
                f"{', '.join(repr(m) for m in SUPPORTED_DEPLOYMENT_MODES)} "
                "-- 'Serverless' and 'RawDeployment' are the pre-0.20 names "
                "for 'Knative' and 'Standard' and still work."
            )
        if self.min_replicas is not None and self.min_replicas < 0:
            raise ValueError(
                f"min_replicas={self.min_replicas} is negative; 0 is "
                "scale-to-zero and the lowest meaningful value."
            )
        if self.max_replicas is not None and self.max_replicas < 1:
            raise ValueError(
                f"max_replicas={self.max_replicas} must be at least 1 — a "
                "ceiling of 0 would leave nothing able to serve."
            )
        if (
            self.min_replicas is not None
            and self.max_replicas is not None
            and self.max_replicas < self.min_replicas
        ):
            raise ValueError(
                f"max_replicas={self.max_replicas} is below "
                f"min_replicas={self.min_replicas}, so the autoscaler has no "
                "range to work in."
            )
        if self.canary_traffic_percent is not None and not (
            0 <= self.canary_traffic_percent <= 100
        ):
            raise ValueError(
                f"canary_traffic_percent={self.canary_traffic_percent} is not "
                "a percentage between 0 and 100."
            )
        if self.predictor_service_port < 1:
            raise ValueError(
                f"predictor_service_port={self.predictor_service_port} is not a port number."
            )
        if self.predictor_container_port < 1:
            raise ValueError(
                f"predictor_container_port={self.predictor_container_port} is not a port number."
            )
        # Empty is allowed and means "the runtime serves /v1 at the root",
        # which is what a non-huggingface runtime may well do. Anything
        # else has to be a path, since it is joined to the endpoint and
        # then to '/v1/chat/completions'.
        if self.openai_path_prefix and not self.openai_path_prefix.startswith("/"):
            raise ValueError(
                f"openai_path_prefix={self.openai_path_prefix!r} must start "
                "with '/', or be empty for a runtime that serves '/v1' at "
                "the root — it is joined to the endpoint to form the chat "
                "route."
            )
        if self.openai_path_prefix.endswith("/"):
            raise ValueError(
                f"openai_path_prefix={self.openai_path_prefix!r} must not end "
                "with '/' — it is joined directly to '/v1/chat/completions', "
                "which would double the separator."
            )


class KServeLLMOptions(BaseModel):
    """Deployment mechanics for the kserve_llm implementation.

    Why this exists alongside `kserve`: an `InferenceService` is KServe's
    general model API, while an `LLMInferenceService` is the one built for
    LLMs -- vLLM directly rather than through a ServingRuntime, LoRA
    adapters, LLM-aware routing through the Gateway API, and a switch to
    turn off the storage-initializer so weights can be streamed. The two
    are different resources with different prerequisites and a different
    endpoint, which is why this is a type of its own rather than a flag on
    `kserve`.

    There is no ServingRuntime here. The model server is vLLM in a pod
    template KServe merges over its own preset (`kserve-config-llm-
    template`), so a GPU image of our own -- the ROCm build for the AMD box
    -- goes in `Inference.image` rather than in a runtime.
    """

    model_config = _KSERVE_MODEL_CONFIG

    # How it is exposed. "none" leaves the workload Service KServe creates
    # (`<name>-kserve-workload-svc`), which is what an in-cluster gateway
    # needs. "gateway" also asks KServe for a managed HTTPRoute on a
    # Gateway API Gateway -- `gateway_name` or KServe's default one.
    router: str = "none"
    gateway_name: str | None = None
    gateway_namespace: str | None = None

    # The Inference Extension's endpoint picker in front of the replicas.
    # Only with router="gateway"; it routes by KV-cache and queue state
    # rather than round robin, which pays off with several replicas.
    scheduler: bool = False

    # Whether KServe serves this over TLS. It must match the cluster's own
    # `enableLLMInferenceServiceTLS` setting in inferenceservice-config:
    # KServe's preset probes and serves HTTPS when that is on, so a
    # mismatch here gives the wrong scheme on the endpoint and, in stream
    # mode, a server the probes cannot reach. check_prerequisites compares.
    tls: bool = False

    # The port vLLM listens on in KServe's preset, and that the workload
    # Service exposes. Fixed by the preset's own `--port 8000`.
    service_port: int = 8000

    # LLMInferenceServiceConfig names merged under this one, last wins --
    # for a cluster that keeps its own templates.
    base_refs: list[str] = Field(default_factory=list)

    # Attached to the pod so KServe's storage-initializer can read a
    # private bucket. None makes the driver create `<name>-storage` bound to
    # `Inference.s3_secret_name`, as the kserve driver does.
    service_account_name: str | None = None

    # Merged over KServe's preset securityContext on the `main` container,
    # and set as the pod's securityContext. The preset sets runAsNonRoot,
    # so an image that runs as root (the ROCm vLLM builds do) is refused at
    # start -- "image will run as root" -- unless a numeric runAsUser is
    # given here. On AMD that user also needs the host groups owning
    # /dev/kfd and /dev/dri (video, render) as the pod's supplementalGroups.
    security_context: dict[str, Any] | None = None
    pod_security_context: dict[str, Any] | None = None

    platform: KServePlatform = Field(default_factory=KServePlatform)

    ready_timeout: int = 1800
    warmup_timeout: int = 600

    def validate(self) -> None:
        if self.router not in SUPPORTED_LLM_ROUTERS:
            raise ValueError(f"router={self.router!r} is not one of {SUPPORTED_LLM_ROUTERS}.")
        if self.scheduler and self.router != "gateway":
            raise ValueError(
                "scheduler=True needs router='gateway' -- the endpoint picker "
                "sits behind a Gateway API route, and with router='none' "
                "there is no route for it to serve."
            )
        if (self.gateway_name or self.gateway_namespace) and self.router != "gateway":
            raise ValueError(
                "gateway_name/gateway_namespace are set but router is "
                f"{self.router!r}, so no route would use them."
            )
        if self.gateway_namespace and not self.gateway_name:
            raise ValueError(
                "gateway_namespace is set without gateway_name; name the "
                "Gateway, or leave both unset for KServe's default."
            )
        if self.service_port < 1:
            raise ValueError(f"service_port={self.service_port} is not a port.")


class Inference(CapabilitySpec):
    """One inference deployment serving an OpenAI-compatible API."""

    CAPABILITY: ClassVar[str] = "inference"
    SUPPORTED_TYPES: ClassVar[tuple[str, ...]] = SUPPORTED_TYPES
    OPTIONS_FOR_TYPE: ClassVar[dict[str, type]] = {
        "vllm": VLLMOptions,
        "llamacpp": LlamaCppOptions,
        "kserve": KServeOptions,
        "kserve_llm": KServeLLMOptions,
    }
    DEFAULT_NAMESPACES: ClassVar[dict[str, str]] = {
        "vllm": "inference",
        "llamacpp": "inference",
        "kserve": "inference",
        "kserve_llm": "inference",
    }
    REQUIRES: ClassVar[tuple[str, ...]] = ("cluster",)

    FROM_STACK: ClassVar[dict[str, str]] = {
        "kubeconfig_path": "kubeconfig_path",
        # Pulled from a storage capability elsewhere in the stack, if one
        # provisioned the bucket/credentials this deployment mirrors from.
        "s3_endpoint_url": "s3_endpoint_url",
        "s3_secret_name": "s3_secret_name",
    }
    # The in-cluster endpoint a gateway's upstream_url can point at.
    PROVIDES: ClassVar[dict[str, str]] = {"inference_endpoint": "endpoint"}

    type: str = "vllm"
    kubeconfig_path: str
    namespace: str | None = None
    # A Union, so the type discriminator in CapabilitySpec resolves a
    # dict to the right model -- see its own note on why best-fit
    # resolution silently produced the wrong one for Policy.
    options: VLLMOptions | LlamaCppOptions | KServeOptions | KServeLLMOptions | None = None

    # The model to serve. Left open for the caller — see module docstring.
    model: str = DEFAULT_MODEL

    name: str = "vllm"
    replicas: int = 1
    port: int = 8000

    # Which extended resource a GPU is called on this cluster. NVIDIA's
    # device plugin advertises `nvidia.com/gpu`, AMD's ROCm plugin
    # advertises `amd.com/gpu`, and nothing makes one of them canonical.
    # Hardcoding NVIDIA meant device="gpu" on an AMD cluster requested a
    # resource no node had: the pod stayed Pending forever and the only
    # explanation was "Insufficient nvidia.com/gpu", naming a vendor that
    # cluster does not run. Verified on a Radeon 8060S box whose plugin
    # advertises amd.com/gpu=1.
    gpu_resource: str = "nvidia.com/gpu"

    device: str = "cpu"
    # None means "decide from the target node's CPU flags" — see
    # VLLMDriver.recommended_dtype. Otherwise vLLM takes the dtype from the
    # model config, which is how a bfloat16 model gets emulated on a CPU
    # with no bf16 support.
    dtype: str | None = None
    max_model_len: int = 4096
    gpu_count: int = 0

    # requests == limits (Guaranteed QoS): a cpu limit below the OpenMP
    # thread count makes cgroup throttling fight the thread pool.
    cpu_cores: int = 6
    memory_gb: int = 12
    kv_cache_gb: int = 4  # VLLM_CPU_KVCACHE_SPACE, counted in memory_gb.
    # k8s defaults /dev/shm to 64Mi; vLLM's shm queue needs ~160Mi minimum.
    shm_gb: int = 2

    node_selector: dict[str, str] = Field(default_factory=dict)
    tolerations: list[dict[str, Any]] = Field(default_factory=list)

    # Serves on the node's own address instead of a pod IP — a workaround
    # for reaching the pod from outside the cluster. Off by default: the
    # gateway consumes this in-cluster, where the plain Service is right.
    host_network: bool = False

    # Points at an existing S3-compatible bucket holding the model weights —
    # this capability only mirrors from it, it never creates the bucket.
    s3_endpoint_url: str | None = None
    # Name of an existing Secret with AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY
    # keys; mounted into the mirror init container's env (see driver).
    s3_secret_name: str | None = None
    # Skip TLS verification against s3_endpoint_url — for self-signed/dev
    # object stores, not for production endpoints.
    s3_insecure_tls: bool = False

    # Where the serving copy of the weights lives (MODEL_CACHE_MODES). A
    # spec-level field, not an option, because it means the same thing to
    # every implementation -- only how it is done differs:
    #
    #             vllm                     llamacpp      kserve / kserve_llm
    #   none      temporary pod folder     node folder*  storage-initializer
    #   node      node folder + marker     node folder   LocalModelCache
    #   stream    runai streamer           --            runai streamer
    #                                                    (kserve_llm only)
    #
    # * llama.cpp has always kept its GGUFs on the node; "none" leaves that
    #   as it was rather than taking a working cache away.
    #
    # "none" is the default, so no existing deployment changes.
    model_cache: str = "none"

    # kserve / kserve_llm with model_cache="node" only (CACHE_SCOPES). None
    # means "namespace" there -- see resolved_cache_scope. Private by
    # default because a cluster cache is shared by every namespace that
    # deploys the same model: one download account and Secret for all of
    # them, and any one deployment's remove_cache deletes it for the rest.
    # Seen on a live cluster.
    cache_scope: str | None = None

    # kserve / kserve_llm only: the LocalModelNodeGroup holding the copy.
    # None means the platform's own `model_cache.node_group`.
    cache_node_group: str | None = None

    # The model's size, which KServe requires on a LocalModelCache so it can
    # refuse one that would not fit. kserve / kserve_llm with "node" only.
    cache_size_gb: int | None = None

    # vllm / llamacpp only: the host folder for model_cache="node".
    # None means DEFAULT_NODE_CACHE_PATH for vllm, and llamacpp's own
    # `model_cache_path` option.
    cache_path: str | None = None

    served_model_name: str | None = None

    # matchLabels selectors (e.g. [{"app": "model-gateway"}]) allowed to
    # reach this service on `port`. Empty => no NetworkPolicy is rendered
    # at all, so an existing deployment is untouched until a caller opts
    # in (ADR-006: features attach by config, not code).
    allowed_client_labels: list[dict[str, str]] = Field(default_factory=list)

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
    service_monitor_labels: dict[str, str] = Field(default_factory=dict)

    # First request after readiness is ~an order of magnitude slower than
    # steady state; this absorbs that cost at deploy time instead.
    warmup: bool = True

    runtime_class: str | None = None
    image: str | None = None
    extra_args: list[str] = Field(default_factory=list)

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
        self._validate_model_cache()
        if self.type == "kserve_llm" and self.service_monitor_labels:
            # Refused rather than ignored: a ServiceMonitor matches Service
            # labels, and the labels KServe puts on an LLMInferenceService's
            # workload Service have not been confirmed against a live 0.20
            # cluster. Guessing produces the silent droppedTargets failure
            # the vllm driver documents.
            raise ValueError(
                "service_monitor_labels is not supported for type='kserve_llm' "
                "yet; scrape the workload Service with a ServiceMonitor of "
                "your own."
            )

    def _validate_model_cache(self) -> None:
        """The model_cache fields, each refused where it would be ignored.

        A cache setting that silently does nothing is the failure this
        guards against: a caller who asked for a namespace-private cache on
        vllm would otherwise believe their weights were isolated.
        """
        is_kserve = self.type in KSERVE_TYPES
        if self.model_cache not in MODEL_CACHE_MODES:
            raise ValueError(f"model_cache={self.model_cache!r} is not one of {MODEL_CACHE_MODES}.")
        if self.cache_scope is not None and self.cache_scope not in CACHE_SCOPES:
            raise ValueError(f"cache_scope={self.cache_scope!r} is not one of {CACHE_SCOPES}.")

        if self.model_cache == "stream":
            if self.type == "llamacpp":
                raise ValueError(
                    "model_cache='stream' is not available for llamacpp: it "
                    "fetches GGUF files itself from a -hf reference, and the "
                    "streamer reads safetensors. Use 'node' (llama.cpp always "
                    "caches on the node anyway)."
                )
            if self.type == "kserve":
                raise ValueError(
                    "model_cache='stream' is not available for type='kserve': "
                    "an InferenceService always runs KServe's storage-"
                    "initializer, so the weights would be downloaded anyway. "
                    "Use type='kserve_llm', whose storage-initializer can be "
                    "turned off."
                )
            if not self.model_is_s3:
                raise ValueError(
                    "model_cache='stream' needs an s3:// model -- the "
                    f"streamer reads from object storage, and {self.model!r} "
                    "is not an object-store path."
                )

        if not is_kserve:
            if self.cache_scope is not None:
                raise ValueError(
                    f"cache_scope={self.cache_scope!r} only applies to KServe "
                    f"types; {self.type} keeps its cache on the node, where "
                    "any pod scheduled there can read it."
                )
            for name in ("cache_node_group", "cache_size_gb"):
                if getattr(self, name) is not None:
                    raise ValueError(
                        f"{name} only applies to KServe types, whose cache is "
                        f"a LocalModelCache; {self.type} uses cache_path."
                    )
        else:
            if self.cache_path is not None:
                raise ValueError(
                    "cache_path does not apply to KServe types -- where the "
                    "copy lives is set once on the platform, in "
                    "KServePlatform.model_cache.path."
                )
            if self.model_cache != "node" and (
                self.cache_node_group is not None
                or self.cache_size_gb is not None
                or self.cache_scope is not None
            ):
                raise ValueError(
                    "cache_node_group, cache_size_gb and cache_scope only "
                    f"apply to model_cache='node', and it is "
                    f"{self.model_cache!r}."
                )
            if self.model_cache == "node" and not self.cache_size_gb:
                raise ValueError(
                    "model_cache='node' on a KServe type needs cache_size_gb: "
                    "KServe requires a model size on every LocalModelCache, so "
                    "it can refuse one the node group has no room for."
                )
            if self.cache_size_gb is not None and self.cache_size_gb < 1:
                raise ValueError(f"cache_size_gb={self.cache_size_gb} must be at least 1.")

        if self.cache_path is not None:
            if self.model_cache != "node" and self.type != "llamacpp":
                raise ValueError(
                    "cache_path is set but model_cache is "
                    f"{self.model_cache!r}; it only applies to 'node'."
                )
            if not self.cache_path.startswith("/"):
                raise ValueError(
                    f"cache_path={self.cache_path!r} must be an absolute path on the host."
                )

    @property
    def node_cache_path(self) -> str:
        """The host folder vllm keeps its node cache in."""
        return self.cache_path or DEFAULT_NODE_CACHE_PATH

    @property
    def model_cache_key(self) -> str:
        """Where this model sits inside a node cache: the object path.

        The bucket and prefix rather than the served name, so two
        deployments of different versions -- `qwen/v1` and `qwen/v2`, both
        served as "qwen" -- never share a folder, and two deployments of
        the same version always do.
        """
        return self.model[len("s3://") :].strip("/")

    @property
    def resolved_cache_scope(self) -> str:
        """`cache_scope`, or "namespace" when unset. KServe types only."""
        return self.cache_scope or "namespace"

    @property
    def model_is_s3(self) -> bool:
        """True when `model` names an s3:// object rather than a HuggingFace
        repo id — switches the driver to mirror weights locally first
        instead of letting vLLM pull from the HF cache."""
        return self.model.startswith("s3://")

    @property
    def served_name(self) -> str:
        """The model id clients pass to the OpenAI API.

        Not the same thing for both implementations, which is what this
        property existed to hide and did not. vLLM is told what to call
        the model (`--served-model-name`), so it answers to the last
        segment of `model` -- "Qwen2.5-0.5B-Instruct". KServe is not:
        its OpenAI endpoint answers to the InferenceService's own name
        and nothing else, so the same deployment serves "kserve-qwen".

        Returning the vLLM answer for both is what broke the org
        portal's chat page on 2026-10-12. The gateway advertised
        "Qwen2.5-0.5B-Instruct" in GET /v1/models, the picker offered it
        as the only choice, and the completion came back
        `404 Model with name Qwen2.5-0.5B-Instruct does not exist` --
        from KServe, which only knows "kserve-qwen". Every
        OpenAI-compatible client discovers then uses, so this broke all
        of them, not just the chat page.

        `served_model_name` still wins when set: the KServe driver passes
        it to the runtime as `--model_name`, and the runtime then answers
        to that name and no other. Confirmed on a live KServe 0.20 cluster
        on 2026-10-13 -- an InferenceService "name-check" with
        served_model_name="custom-qwen" advertised only "custom-qwen" and
        returned 404 for "name-check". Ignoring the override here would
        publish the one name the runtime rejects.

        For vLLM it is the last path segment, unless that segment is a
        version: weights are kept under immutable `<model>/<version>/`
        paths, and `s3://models/qwen/v1` should serve as "qwen", not
        "v1" -- which every versioned model would otherwise share.
        """
        if self.type in KSERVE_TYPES:
            return self.served_model_name or self.name
        if self.served_model_name:
            return self.served_model_name
        segments = [s for s in self.model.rstrip("/").split("/") if s]
        if len(segments) > 1 and VERSION_SEGMENT.match(segments[-1]):
            return segments[-2]
        return segments[-1]

    @property
    def model_path(self) -> str:
        """What vLLM is told to load.

        The object-store URI itself when streaming, since the streamer reads
        it directly. A folder keyed by the object path under a node cache,
        so versions never collide. Otherwise the per-pod folder the mirror
        fills, as before.
        """
        if not self.model_is_s3 or self.model_cache == "stream":
            return self.model
        if self.model_cache == "node":
            return f"{LOCAL_MODEL_DIR}/{self.model_cache_key}"
        return f"{LOCAL_MODEL_DIR}/{self.served_name}"

    @property
    def container_image(self) -> str:
        if self.image:
            return self.image
        return DEFAULT_GPU_IMAGE if self.device == "gpu" else DEFAULT_CPU_IMAGE

    @property
    def service_name(self) -> str:
        """The Service callers reach this on.

        For vllm and llamacpp that is the deployment's own name, because
        those drivers render the Service themselves. KServe does not
        hand that over: an `InferenceService` named `x` produces a
        Service named `x-predictor`, and the name is KServe's to decide
        rather than ours to pass in. Branching here rather than in the
        drivers is deliberate — `PROVIDES` publishes `endpoint` from the
        *spec*, so a Stack wiring a gateway to this would otherwise
        publish a URL that resolves to nothing.
        """
        if self.type == "kserve":
            return f"{self.name}-predictor"
        if self.type == "kserve_llm":
            # KServe's own name for the Service in front of an
            # LLMInferenceService's workload (kmeta.ChildName(name,
            # "-kserve-workload-svc") in the llmisvc controller).
            return f"{self.name}-kserve-workload-svc"
        return self.name

    @property
    def service_port(self) -> int:
        """The port that Service listens on, which is not always the port
        the container serves. KServe fixes its predictor Service at 80 and
        derives the container port itself."""
        if self.type == "kserve":
            return self.options.predictor_service_port
        if self.type == "kserve_llm":
            return self.options.service_port
        return self.port

    @property
    def scheme(self) -> str:
        """https only where KServe serves an LLMInferenceService over TLS."""
        if self.type == "kserve_llm" and self.options.tls:
            return "https"
        return "http"

    @property
    def base_path(self) -> str:
        """Path the OpenAI routes hang off, appended to the endpoint.

        Empty for vllm and llamacpp, which serve `/v1` at the root of
        their port. KServe's huggingface runtime does not: it mounts the
        OpenAI routes under a prefix and keeps `/v1` for KServe's own
        predict protocol, so `/v1/chat/completions` there is not an
        unmounted route that might work by accident -- it belongs to
        something else.

        This belongs on `endpoint` rather than in the driver because
        `PROVIDES` publishes `endpoint` from the spec, and every consumer
        treats it as a base to append `/v1/chat/completions` to --
        model-gateway hardcodes exactly that. Publishing the bare Service
        URL meant a gateway wired to a default KServe deployment asked
        for a path the runtime does not serve, and every real request
        404'd against a deployment that was otherwise healthy.
        """
        if self.type == "kserve":
            return self.options.openai_path_prefix
        return ""

    @property
    def endpoint(self) -> str:
        """The in-cluster base URL — what a gateway's upstream_url points
        at. With host_network, the pod's real (node-IP) address is only
        knowable from the cluster, so the driver's own `endpoint()` method
        is the source of truth in that case; this stays the static form."""
        return (
            f"{self.scheme}://{self.service_name}.{self.resolved_namespace}"
            f".svc.cluster.local:{self.service_port}{self.base_path}"
        )

    def container_args(self, dtype: str | None = None) -> list[str]:
        args = [
            "--model",
            self.model_path,
            "--max-model-len",
            str(self.max_model_len),
            "--host",
            "0.0.0.0",
            "--port",
            str(self.port),
        ]
        # Every s3:// model, not only a mirrored one: streaming passes the
        # URI itself as the model, and clients should not have to send
        # "s3://models/qwen/v1" as the model name.
        if self.model_is_s3:
            args += ["--served-model-name", self.served_name]
        if self.model_cache == "stream":
            args += ["--load-format", "runai_streamer"]
        resolved = dtype or self.dtype
        if resolved:
            args += ["--dtype", resolved]
        if self.device == "gpu" and self.gpu_count > 1:
            args += ["--tensor-parallel-size", str(self.gpu_count)]
        return args + list(self.extra_args)

    def env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        if self.device == "cpu":
            env["VLLM_CPU_KVCACHE_SPACE"] = str(self.kv_cache_gb)
            env["OMP_NUM_THREADS"] = str(self.cpu_cores)
            env["VLLM_CPU_OMP_THREADS_BIND"] = f"0-{self.cpu_cores - 1}"
        return env

    def stream_env(self) -> dict[str, str]:
        """Settings the Run:ai streamer needs to read from SeaweedFS.

        Non-secret values only; the credentials come from s3_secret_name
        through secretKeyRef in the driver, never as literals here.

        Path-style addressing is the one that matters: the streamer
        defaults to virtual-host style (`bucket.host`), which an
        S3-compatible store such as SeaweedFS does not serve -- the same
        reason examples/full_stack.py configures boto3 with path style.
        The metadata switch skips the EC2 credential probe, as in
        mirror_command(); a region is required by the client even though
        this endpoint ignores it.
        """
        if self.model_cache != "stream":
            return {}
        return {
            "AWS_ENDPOINT_URL": self.s3_endpoint_url or "",
            "RUNAI_STREAMER_S3_USE_VIRTUAL_ADDRESSING": "0",
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_DEFAULT_REGION": "us-east-1",
        }

    def mirror_command(self) -> list[str]:
        """Shell command for the `fetch-weights` init container: syncs
        every object under the model's S3 prefix down to model_path,
        reading AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY from the env
        (sourced from s3_secret_name by the driver). Read-only against
        the bucket — nothing is created or uploaded.

        The AWS CLI (DEFAULT_MIRROR_IMAGE), not boto3 installed at
        deploy time: an earlier version of this (2026-09-25, replacing
        `mc` — the MinIO Client CLI, which MinIO stopped distributing
        that day) ran `pip install boto3` in the init container itself,
        which put PyPI back on the critical path for every deploy —
        exactly the live-internet dependency and floating-version
        problem digest-pinning DEFAULT_MIRROR_IMAGE was meant to remove,
        just moved from the model image to this one. `aws s3 sync` needs
        nothing installed at runtime.

        AWS_EC2_METADATA_DISABLED unset would otherwise cost this a real
        wait per invocation: the CLI's default credential chain probes
        the EC2 instance-metadata endpoint before falling through to the
        env vars above, and that endpoint doesn't exist here. A region is
        still required even against a non-AWS endpoint — the CLI doesn't
        know that this endpoint doesn't need one — so one is set and
        otherwise unused.
        """
        verify_flag = "--no-verify-ssl" if self.s3_insecure_tls else ""
        remote = self.model[len("s3://") :]
        bucket, _, prefix = remote.partition("/")
        if self.model_cache == "node":
            return self._node_cache_command(verify_flag, bucket, prefix)
        script = f'''set -e
export AWS_EC2_METADATA_DISABLED=true
export AWS_DEFAULT_REGION=us-east-1
mkdir -p "{self.model_path}"
aws --endpoint-url "{self.s3_endpoint_url}" {verify_flag} s3 sync \\
    "s3://{bucket}/{prefix}" "{self.model_path}" --no-progress
count=$(find "{self.model_path}" -type f | wc -l)
if [ "$count" -eq 0 ]; then
    echo "no objects under s3://{bucket}/{prefix}" >&2
    exit 1
fi
ls -l "{self.model_path}"'''
        return ["sh", "-c", script]

    def _node_cache_command(self, verify_flag: str, bucket: str, prefix: str) -> list[str]:
        """The mirror for model_cache="node": download once per machine.

        Three rules, each for a failure a plain `aws s3 sync` into the cache
        folder would have:

          * **A marker decides "already here", not the files.** It holds
            the source URI and is written last, so a folder with a marker
            is a finished copy of exactly this model. A restart or a reboot
            finds it and starts at once, with no listing of the bucket.
          * **Download beside, then rename.** The copy lands in a folder
            of its own and is renamed into place only once complete, so a
            pod killed mid-download -- a power cut, an eviction -- leaves
            nothing a later start could mistake for a model.
          * **One temporary folder per pod.** Two replicas starting on the
            same machine each download into their own, and whichever
            finishes second sees the first one's marker and throws its
            copy away rather than renaming over a model in use.
        """
        dest = self.model_path
        marker = f"{dest}/{CACHE_MARKER}"
        script = f'''set -e
export AWS_EC2_METADATA_DISABLED=true
export AWS_DEFAULT_REGION=us-east-1
dest="{dest}"
marker="{marker}"
source="{self.model}"
if [ -f "$marker" ] && [ "$(cat "$marker")" = "$source" ]; then
    echo "using cached $source"
    exit 0
fi
partial="$dest.partial-$HOSTNAME"
rm -rf "$partial"
mkdir -p "$partial"
aws --endpoint-url "{self.s3_endpoint_url}" {verify_flag} s3 sync \\
    "s3://{bucket}/{prefix}" "$partial" --no-progress
count=$(find "$partial" -type f | wc -l)
if [ "$count" -eq 0 ]; then
    rm -rf "$partial"
    echo "no objects under s3://{bucket}/{prefix}" >&2
    exit 1
fi
if [ -f "$marker" ] && [ "$(cat "$marker")" = "$source" ]; then
    rm -rf "$partial"
    echo "another pod cached $source first; using its copy"
    exit 0
fi
rm -rf "$dest"
mkdir -p "$(dirname "$dest")"
mv "$partial" "$dest"
echo "$source" > "$marker"
echo "cached $source"'''
        return ["sh", "-c", script]
