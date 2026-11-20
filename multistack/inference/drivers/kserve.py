"""
The KServe implementation: hands an `InferenceService` to a KServe that is
already installed, rather than rendering a Deployment and Service directly.

The difference from the other two drivers is worth stating, because it is
what every method below follows from. `vllm` and `llamacpp` own their
workload: they render a Deployment, a Service and the pod spec, apply them,
and what the cluster holds is what this SDK put there. This driver owns an
`InferenceService` and nothing else -- KServe's controller produces the
Deployment, the Service, the autoscaler and the model download from it.

Three consequences run through this file:

  * **The Service is named by KServe**, `<name>-predictor`, which is why
    `Inference.service_name` branches on type rather than the driver
    passing a name in. A Stack publishes `endpoint` from the spec.
  * **Weights are KServe's job**, so there is no mirror init container
    here: `s3://` and `hf://` go into `storageUri` and the
    storage-initializer fetches them.
  * **KServe must already exist.** This driver deploys onto it and never
    installs it, so `check_prerequisites` is written to say exactly what
    is missing before anything is applied -- an `InferenceService` on a
    cluster without the CRD fails loudly, but one in Serverless mode on a
    cluster without Knative is accepted and then simply never goes ready.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from typing import Any

from ...kube import apply as kube_apply
from ...kube import kubectl as kube_kubectl
from ...kube import require_cli, require_cluster
from ...netpolicy import network_policy_manifest
from ...servicemonitor import service_monitor_manifest
from ..base import InferenceError, InferencePrerequisiteError
from ..spec import DEFAULT_OPENAI_PATH_PREFIX, Inference
from .kserve_platform import ISVC_CONFIGMAP, KServePlatformInstaller

# The CRD this driver writes. Its absence is the one prerequisite failure
# that cannot be worked around, so it is checked first and by name.
ISVC_CRD = "inferenceservices.serving.kserve.io"
ISVC_API_VERSION = "serving.kserve.io/v1beta1"

# Knative Serving's own CRD, needed only for the Knative deployment mode
# (spelled "Serverless" before KServe 0.20).
KNATIVE_CRD = "services.serving.knative.dev"

# Where each ingress option's front door lives. Serverless traffic
# reaches a model through it, and KServe will not report an
# InferenceService ready until it has an address -- see
# KServeDriver._check_knative_ingress.
KNATIVE_INGRESS_SERVICE = {
    "kourier": ("kourier", "knative-serving"),
    "istio": ("istio-ingressgateway", "istio-system"),
}

# The label KServe puts on everything it generates from an
# InferenceService -- the pods a NetworkPolicy selects and the Service a
# ServiceMonitor matches. Not `app: <name>`: that is the other drivers'
# label, and nothing applies it here.
ISVC_LABEL = "serving.kserve.io/inferenceservice"

DEPLOYMENT_MODE_ANNOTATION = "serving.kserve.io/deploymentMode"

# Prefix of the annotations KServe reads off a Secret to configure its S3
# storage-initializer (endpoint, region, TLS). A Secret without any of
# them is the quiet one: the ServiceAccount binds, the pod schedules, and
# the init container fails on a default endpoint minutes later.
S3_ANNOTATION_PREFIX = "serving.kserve.io/s3-"

NVIDIA_GPU_RESOURCE = "nvidia.com/gpu"

LOCALMODEL_API_VERSION = "serving.kserve.io/v1alpha1"
LOCALMODEL_CACHE_CRD = "localmodelcaches.serving.kserve.io"
# Recorded on the cache so a person listing caches can see which model a
# hashed name stands for.
CACHE_SOURCE_ANNOTATION = "multistack.io/source-model-uri"
# The object store that URI was read from. KServe keys the copy on disk by
# the URI alone, so this is what tells two stores' same-named models apart.
CACHE_ENDPOINT_ANNOTATION = "multistack.io/source-endpoint"
# On the download account and Secret copy in KServe's job namespace, naming
# the cache they belong to.
CACHE_CREDENTIALS_LABEL = "multistack.io/model-cache"


class KServeDriver:
    """Deploys and removes KServe-backed inference services."""

    error_cls = InferenceError
    prerequisite_error_cls = InferencePrerequisiteError
    log_prefix = "inference/kserve"
    # The resource this driver owns. A class attribute so the readiness and
    # endpoint logic below serves the kserve_llm driver unchanged.
    resource_kind = "inferenceservice"

    def __init__(self, command_timeout: int = 300) -> None:
        self.command_timeout = command_timeout

    # -- checks ------------------------------------------------------------
    def check_prerequisites(self, inference: Inference, nodes: list | None = None) -> list[str]:
        """What must be on the cluster before an InferenceService is worth
        applying. Raises on the things that cannot succeed; returns
        warnings for the things that merely often go wrong."""
        warnings: list[str] = []
        require_cli("kubectl", error_cls=InferencePrerequisiteError)
        require_cluster(inference.kubeconfig_path, capability="inference")
        options = inference.options

        installs = options.platform.install

        if not self._exists(inference, "crd", ISVC_CRD):
            if not installs:
                raise InferencePrerequisiteError(
                    f"this cluster has no {ISVC_CRD} CRD, so KServe is not "
                    "installed and an InferenceService cannot be created. "
                    "platform.install is False, so this driver will not "
                    "install it — install KServe by hand, or set "
                    "KServePlatform(install=True)."
                )
            # create() installs it, so this is a statement of what will
            # happen rather than a reason to stop. Every check below it
            # reads a cluster that does not have KServe yet, so there is
            # nothing further worth asserting on this pass.
            return [
                *warnings,
                "KServe is not installed yet; create() will install it "
                "(cert-manager, CRDs and controller) before deploying.",
            ]

        if options.uses_knative:
            if not self._exists(inference, "crd", KNATIVE_CRD):
                if not installs:
                    raise InferencePrerequisiteError(
                        "deployment_mode='Serverless' needs Knative Serving, "
                        f"but this cluster has no {KNATIVE_CRD} CRD. KServe "
                        "would accept the InferenceService and it would never "
                        "become ready, with the reason only in the KServe "
                        "controller's log. Install Knative, or use "
                        "deployment_mode='RawDeployment'."
                    )
                warnings.append(
                    "Knative Serving is not installed yet; create() will "
                    "install the Knative operator and reconcile a "
                    "KnativeServing resource with "
                    f"{options.platform.knative_ingress} ingress."
                )
            else:
                warnings.extend(self._check_knative_ingress(inference))

        if options.runtime and not self._exists(
            inference, "clusterservingruntime", options.runtime
        ):
            raise InferencePrerequisiteError(
                f"no ClusterServingRuntime named '{options.runtime}' exists "
                "on this cluster, so KServe has nothing to run the model "
                "with. Leave `runtime` unset to let KServe select one from "
                f"model_format='{options.model_format}'."
            )

        if options.min_replicas == 0 and not options.uses_knative:
            warnings.append(
                f"min_replicas=0 (scale-to-zero) in "
                f"{options.canonical_deployment_mode} mode needs "
                "KEDA and a recent KServe. Without them the deployment "
                "simply stays at one replica — which is safe, but not what "
                "was asked for. Serverless mode scales to zero natively."
            )

        if inference.device == "gpu":
            warnings.extend(self._check_gpu_resource(inference))
            warnings.extend(self._check_gpu_runtime_class(inference))

        if inference.service_monitor_labels:
            warnings.extend(self._check_metrics_port(inference))

        # Not gated on service_monitor_labels: the port name only matters to a
        # ServiceMonitor, but the path prefix matters to every caller of the
        # endpoint whether or not anything is scraping it.
        warnings.extend(self._check_openai_path_prefix(inference))

        if inference.model_is_s3:
            warnings.extend(self._check_s3_secret(inference))

        if inference.model_cache == "node":
            warnings.extend(self._check_model_cache(inference))

        return warnings

    def _check_model_cache(self, inference: Inference) -> list[str]:
        """What a LocalModelCache needs before it can fill.

        Each of these fails quietly on the cluster: a cache naming a node
        group that does not exist, or whose download Job cannot read the
        bucket, is accepted and simply never becomes ready -- and the
        deployment above it then downloads the model the old way, so it
        works, slowly, with nothing saying why.
        """
        warnings: list[str] = []
        platform = inference.options.platform
        if not self._exists(inference, "crd", LOCALMODEL_CACHE_CRD):
            if not platform.install:
                raise InferencePrerequisiteError(
                    "model_cache='node' needs KServe's local model cache, but "
                    f"this cluster has no {LOCALMODEL_CACHE_CRD} CRD and "
                    "platform.install is False. Install it by hand, or set "
                    "KServePlatform(install=True) with model_cache enabled."
                )
            return warnings
        if not platform.install and not self._local_model_enabled(inference):
            # With install=True, create() switches it on; without, nothing
            # will, and the cache would fill but never be used.
            raise InferencePrerequisiteError(
                "model_cache='node' needs KServe's local model cache switched "
                f"on, but localModel.enabled is not true in {ISVC_CONFIGMAP} "
                f"in {platform.namespace}. As it is, the cache would download "
                "and the model would still fetch its weights on every start. "
                "Set it to true, or use KServePlatform(install=True) to let "
                "this driver do it."
            )
        group = self.cache_node_group(inference)
        if not self._exists(inference, "localmodelnodegroup", group):
            warnings.append(
                f"no LocalModelNodeGroup named {group!r} exists yet, so the "
                "cache has nowhere to land. create() makes it when "
                "KServePlatform.model_cache is enabled with that node_group."
            )
        return warnings

    def _local_model_enabled(self, inference: Inference) -> bool:
        raw = self._kubectl(
            inference,
            "get",
            "configmap",
            ISVC_CONFIGMAP,
            "-n",
            inference.options.platform.namespace,
            "-o",
            "jsonpath={.data.localModel}",
            check=False,
        )
        try:
            return bool(raw.strip()) and json.loads(raw).get("enabled") is True
        except (ValueError, AttributeError):
            return False

    def _check_openai_path_prefix(self, inference: Inference) -> list[str]:
        """Whether the runtime actually serves under `openai_path_prefix`.

        KServe's own huggingfaceserver mounts the OpenAI-compatible routes
        under /openai, which is where the default points. A runtime built
        around vLLM's own OpenAI server mounts them at the root, so the same
        default produces /openai/v1/chat/completions against a server that
        only has /v1/chat/completions -- and the InferenceService goes Ready
        either way, because the predictor itself is healthy. The 404 surfaces
        only at the first real request, from whatever is calling the endpoint
        this SDK published, which is a long way from the cause.

        Observed live on a hand-built ROCm runtime: /v1/models answered 200
        and /openai/v1/models answered 404, so the published endpoint was one
        path segment too deep.

        Only checkable for a pinned runtime, and only worth saying while the
        prefix is still the default -- anyone who set it explicitly has
        already made this decision.
        """
        options = inference.options
        runtime = options.runtime
        if not runtime or options.openai_path_prefix != DEFAULT_OPENAI_PATH_PREFIX:
            return []

        raw = self._kubectl(
            inference,
            "get",
            "clusterservingruntime",
            runtime,
            "-o",
            "json",
            check=False,
        )
        if not raw.strip():
            return []
        try:
            containers = json.loads(raw)["spec"].get("containers") or []
        except (ValueError, KeyError):
            return []

        # The image identifies the built-in runtime, not the runtime's name:
        # a cluster may carry a renamed copy of huggingfaceserver, and a
        # custom runtime may be named anything at all.
        images = " ".join(c.get("image") or "" for c in containers)
        if "huggingfaceserver" in images:
            return []

        return [
            f"ClusterServingRuntime {runtime!r} does not look like KServe's "
            "huggingfaceserver, but openai_path_prefix is still the default "
            f"{DEFAULT_OPENAI_PATH_PREFIX!r}. A runtime running vLLM's own "
            "OpenAI server mounts /v1/... at the root, so the published "
            "endpoint would be one segment too deep and every request through "
            "it would 404 while the InferenceService still reported Ready. "
            "Set openai_path_prefix='' if this runtime serves at the root."
        ]

    def _check_metrics_port(self, inference: Inference) -> list[str]:
        """Whether the runtime names the port a ServiceMonitor will ask for.

        KServe copies the container port's name from the ServingRuntime
        onto the Service it generates, and Prometheus Operator resolves a
        ServiceMonitor endpoint by port *name*. A runtime that declares a
        port without a name therefore produces an unnamed Service port,
        and the ServiceMonitor matches nothing -- it is created, the
        target is discovered, and it lands in droppedTargets with no
        error anywhere.

        Observed live: a custom runtime whose port carried no name
        dropped the target, and naming it made the same ServiceMonitor
        go active immediately. Only checkable for a pinned runtime,
        since that is the only one whose definition can be read here.
        """
        runtime = inference.options.runtime
        if not runtime:
            return []

        raw = self._kubectl(
            inference,
            "get",
            "clusterservingruntime",
            runtime,
            "-o",
            "json",
            check=False,
        )
        if not raw.strip():
            return []
        try:
            containers = json.loads(raw)["spec"].get("containers") or []
        except (ValueError, KeyError):
            return []

        ports = (containers[0].get("ports") if containers else None) or []
        declared = ports[0].get("name") if ports else None
        wanted = inference.options.metrics_port_name or inference.service_name

        if not declared:
            return [
                f"ClusterServingRuntime {runtime!r} declares its container "
                "port without a name, so the Service KServe generates has an "
                f"unnamed port and the ServiceMonitor asking for {wanted!r} "
                "will match nothing — it applies cleanly and the target is "
                "silently dropped. Name the port in the runtime, then set "
                "metrics_port_name to that name."
            ]
        if declared != wanted:
            return [
                f"ClusterServingRuntime {runtime!r} names its port "
                f"{declared!r}, but the ServiceMonitor asks for {wanted!r}, "
                "which resolves to nothing. Set "
                f"metrics_port_name={declared!r}."
            ]
        return []

    def _check_gpu_resource(self, inference: Inference) -> list[str]:
        """Whether any node advertises the GPU resource being requested.

        Without a device plugin the pod is unschedulable and the only
        event says "Insufficient <resource>", which does not hint at what
        to install. Worse on a mixed-vendor estate: asking for
        `nvidia.com/gpu` on a cluster whose plugin advertises
        `amd.com/gpu` produces an error naming a vendor that cluster does
        not run, so the message here names what the cluster *does* have.
        """
        raw = self._kubectl(inference, "get", "nodes", "-o", "json", check=False)
        if not raw.strip():
            return []
        try:
            nodes = json.loads(raw).get("items", [])
        except ValueError:
            return []

        # Every extended resource that looks like an accelerator, so the
        # error can name the one this cluster actually offers.
        advertised: dict[str, int] = {}
        for node in nodes:
            for name, count in (node.get("status", {}).get("allocatable") or {}).items():
                if "gpu" not in name.lower():
                    continue
                try:
                    advertised[name] = advertised.get(name, 0) + int(count)
                except (TypeError, ValueError):
                    continue

        wanted = inference.gpu_resource
        if advertised.get(wanted, 0) > 0:
            return []

        others = {k: v for k, v in advertised.items() if v > 0 and k != wanted}
        if others:
            names = ", ".join(f"{k}={v}" for k, v in sorted(others.items()))
            raise InferencePrerequisiteError(
                f"no node advertises {wanted!r}, but this cluster does "
                f"advertise {names}. The pod would stay Pending with only "
                f"'Insufficient {wanted}' to explain it. Set "
                f"gpu_resource='{sorted(others)[0]}' on the Inference spec."
            )
        raise InferencePrerequisiteError(
            f"no node advertises {wanted!r}, and this cluster advertises no "
            "GPU resource at all, so the predictor pod stays Pending with "
            f"only 'Insufficient {wanted}' to explain it. Install a device "
            "plugin first — multistack.accelerator does it for NVIDIA, and "
            "ROCm's k8s-device-plugin does it for AMD."
        )

    def _check_gpu_runtime_class(self, inference: Inference) -> list[str]:
        """Whether a GPU deployment names the runtime that exposes the card.

        Allocating `nvidia.com/gpu` is only half of it: on a cluster where
        the CUDA devices and libraries are mounted by a separate container
        runtime, a pod that does not ask for that runtime schedules onto
        the GPU node, is charged for the card, and still cannot see it.

        The symptom hides the cause. Observed live on RKE2: vLLM reported
        `No platform detected ... UnspecifiedPlatform` and `Failed to infer
        device type`, which reads like an unsupported card rather than a
        missing runtime — after a four-minute image pull and a crash loop.
        """
        if inference.runtime_class:
            return []
        # NVIDIA's cards are reached through a container runtime that
        # mounts the CUDA devices; AMD's are reached through the device
        # plugin, which injects /dev/kfd and /dev/dri itself and needs no
        # RuntimeClass. Warning about a missing one on AMD would send
        # someone looking for a problem that is not there.
        if not inference.gpu_resource.startswith("nvidia."):
            return []

        available = self._kubectl(
            inference,
            "get",
            "runtimeclass",
            "-o",
            "name",
            check=False,
        )
        candidates = [
            line.split("/", 1)[-1].strip()
            for line in (available or "").splitlines()
            if line.strip()
        ]
        gpu_runtimes = [c for c in candidates if "nvidia" in c]
        if not gpu_runtimes:
            return []

        return [
            f"device='gpu' but runtime_class is unset, and this cluster has "
            f"a RuntimeClass named {gpu_runtimes[0]!r}. The pod will be "
            "allocated the card and still not see it — the runtime, not the "
            "device plugin, is what mounts the CUDA devices and libraries. "
            f"Pass runtime_class={gpu_runtimes[0]!r} on the Inference spec."
        ]

    def _check_knative_ingress(self, inference: Inference) -> list[str]:
        """Whether Knative's ingress has an address yet.

        Serverless routes traffic through that Service, and KServe holds
        the InferenceService at `Waiting for load balancer to be ready`
        until it has an address -- so on a cluster with no LoadBalancer
        provider the deployment never goes ready *even though the model
        is running perfectly*. Observed live: the Revision reported
        Ready=True with the pod serving, while the InferenceService sat
        Unknown until it timed out.

        A warning, not a refusal: the address may be provisioned moments
        later, and a cluster may route Knative some other way.
        """
        name, namespace = KNATIVE_INGRESS_SERVICE.get(
            inference.options.platform.knative_ingress, (None, None)
        )
        if name is None:
            return []

        raw = self._kubectl(
            inference,
            "get",
            "service",
            name,
            "-n",
            namespace,
            "-o",
            "json",
            check=False,
        )
        if not raw.strip():
            return []
        try:
            service = json.loads(raw)
        except ValueError:
            return []

        if service.get("spec", {}).get("type") != "LoadBalancer":
            return []
        if service.get("status", {}).get("loadBalancer", {}).get("ingress"):
            return []

        return [
            f"the Knative ingress Service {namespace}/{name} is a "
            "LoadBalancer with no address yet, so this cluster may have no "
            "LoadBalancer provider. KServe holds a Serverless "
            "InferenceService at 'Waiting for load balancer to be ready' "
            "until it has one — the model starts and serves, but the "
            "deployment never reports ready and create() times out. "
            "Install MetalLB (or equivalent), or use "
            "deployment_mode='RawDeployment'. Switching the gateway to "
            "NodePort does not help: Knative accepts it and the route goes "
            "ready, but KServe still waits for an external address — "
            "confirmed on a live cluster. "
            "KServePlatform.knative_ingress_service_type sets that type "
            "where the operator will keep it, for clusters where it is the "
            "right answer for other reasons."
        ]

    def _check_s3_secret(self, inference: Inference) -> list[str]:
        """KServe configures its S3 storage-initializer from annotations on
        the Secret, which this SDK does not own and cannot add."""
        raw = self._kubectl(
            inference,
            "get",
            "secret",
            inference.s3_secret_name,
            "-n",
            inference.resolved_namespace,
            "-o",
            "json",
            check=False,
        )
        if not raw.strip():
            return [
                f"Secret '{inference.s3_secret_name}' was not found in "
                f"namespace {inference.resolved_namespace}. It must exist "
                "before the predictor starts, or the storage-initializer "
                "fails with no credentials."
            ]
        try:
            annotations = json.loads(raw)["metadata"].get("annotations", {})
        except (ValueError, KeyError):
            return []

        if not any(key.startswith(S3_ANNOTATION_PREFIX) for key in annotations):
            return [
                f"Secret '{inference.s3_secret_name}' carries no "
                f"{S3_ANNOTATION_PREFIX}* annotations, so KServe's "
                "storage-initializer will use its default S3 endpoint "
                f"rather than {inference.s3_endpoint_url}. Annotate the "
                "Secret with serving.kserve.io/s3-endpoint (and "
                "s3-usehttps/s3-region as needed)."
            ]
        return []

    # -- the platform underneath -------------------------------------------
    def install_platform(self, inference: Inference) -> list[str]:
        """Brings the cluster to the point where an InferenceService can
        exist, and reports what it did.

        Separate from `create()` so a caller can provision the platform
        once for a fleet of deployments rather than discovering it on the
        first one. Idempotent: a cluster that already has KServe is left
        exactly as it is, including a release this driver did not create.
        """
        installer = KServePlatformInstaller(
            inference.kubeconfig_path,
            command_timeout=self.command_timeout,
        )
        return installer.ensure(
            inference.options.platform,
            inference.options.deployment_mode,
            model_cache=inference.model_cache == "node",
        )

    # -- the model cache ---------------------------------------------------
    @staticmethod
    def cache_node_group(inference: Inference) -> str:
        return inference.cache_node_group or inference.options.platform.model_cache.node_group

    def cache_name(self, inference: Inference) -> str:
        """The LocalModelCache's name: derived from the model, not the
        deployment.

        A cache belongs to the model, so two deployments of the same model
        version must arrive at the same name and apply the same object
        rather than each owning a copy. A hash because a URI is not a valid
        object name; the same 16 hex characters KServe itself uses to name
        the folder on disk.

        The URI alone, deliberately, even though it does not say which object
        store: KServe 0.20's node agent keys the folder on disk by the URI
        only (`GetStorageKey(SourceModelUri)`) and skips the download when
        that folder exists, so a name that also hashed the endpoint would
        give two caches one folder -- the second store's model served the
        first store's files. Same URI from a different store is refused
        instead; see `_check_cache_source`.
        """
        digest = hashlib.sha256(self.storage_uri(inference).encode()).hexdigest()
        # A namespace cache is named apart from the cluster one. KServe names
        # each cache's volume claim after the cache, so a cluster cache and a
        # namespace cache of the same model, both used in one namespace,
        # otherwise collide on one claim -- found on a live cluster, where
        # the namespace cache's deployment was handed the cluster cache's.
        suffix = "-ns" if inference.resolved_cache_scope == "namespace" else ""
        return f"model-{digest[:16]}{suffix}"

    def cache_service_account(self, inference: Inference) -> str | None:
        """The ServiceAccount the cache's download Job reads the bucket as.

        KServe runs every download Job in its job namespace and looks the
        account up there, for a namespace cache as much as a cluster one --
        an account in the deployment's own namespace is never consulted,
        and the Job runs as `default` with no credentials ("Unable to
        locate credentials"). Found on a live KServe 0.20 cluster.

        One account per cache, bound to a copy of the deployment's Secret of
        the same name (see download_secret_manifest). Per cache rather than
        per Secret name: two organisations' namespaces can each have a
        `seaweedfs-creds` holding different keys, and one shared copy would
        hand one organisation's download the other's credentials.
        """
        if not inference.model_is_s3:
            return None
        if inference.resolved_cache_scope == "namespace":
            name = f"{self.cache_name(inference)}-{inference.resolved_namespace}"
        else:
            name = f"{self.cache_name(inference)}-download"
        return name[:63].rstrip("-")

    def storage_service_account(self, inference: Inference) -> str | None:
        """The ServiceAccount the predictor reads the bucket as."""
        if inference.options.service_account_name:
            return inference.options.service_account_name
        if inference.model_is_s3:
            return f"{inference.name}-storage"
        return None

    def model_cache_manifests(self, inference: Inference) -> list[dict[str, Any]]:
        """The LocalModelCache (or LocalModelNamespaceCache) for this model.

        KServe connects a deployment to its cache by string equality on the
        model URI -- `storageUri` here, `sourceModelUri` there -- so both are
        rendered from the one `storage_uri()`. There is no other link; a
        URI that differs by a trailing slash simply never uses the cache.
        """
        if inference.model_cache != "node":
            return []
        namespaced = inference.resolved_cache_scope == "namespace"
        metadata: dict[str, Any] = {
            "name": self.cache_name(inference),
            "annotations": {
                CACHE_SOURCE_ANNOTATION: self.storage_uri(inference),
                CACHE_ENDPOINT_ANNOTATION: inference.s3_endpoint_url or "",
            },
        }
        if namespaced:
            metadata["namespace"] = inference.resolved_namespace
        spec: dict[str, Any] = {
            "sourceModelUri": self.storage_uri(inference),
            "modelSize": f"{inference.cache_size_gb}Gi",
            "nodeGroups": [self.cache_node_group(inference)],
        }
        service_account = self.cache_service_account(inference)
        if service_account:
            spec["serviceAccountName"] = service_account

        objects: list[dict[str, Any]] = []
        if service_account:
            objects.append(
                {
                    "apiVersion": "v1",
                    "kind": "ServiceAccount",
                    "metadata": {
                        "name": service_account,
                        "namespace": inference.options.platform.model_cache.job_namespace,
                        "labels": {CACHE_CREDENTIALS_LABEL: self.cache_name(inference)},
                    },
                    "secrets": [{"name": service_account}],
                }
            )
        objects.append(
            {
                "apiVersion": LOCALMODEL_API_VERSION,
                "kind": "LocalModelNamespaceCache" if namespaced else "LocalModelCache",
                "metadata": metadata,
                "spec": spec,
            }
        )
        return objects

    def _apply_model_cache_first(self, inference: Inference) -> None:
        """Creates the model's cache and waits until KServe has filled it,
        before anything that reads from it is created.

        Two problems this ordering removes, both seen on a live cluster.
        A model pod created alongside its cache is scheduled at once, mounts
        an empty folder and crash-loops until the download lands. And the
        pod's mount can create the model's folder before KServe's agent
        does -- as root, which the download Job cannot write to (the prep
        Job's setgid folder is the other half of that fix). With the cache
        already downloaded, the model starts once, against a full folder.
        """
        if inference.model_cache != "node":
            return
        self._check_cache_source(inference)
        objects: list[dict[str, Any]] = [
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": inference.resolved_namespace},
            },
        ]
        # The download account and its Secret have to exist before the
        # cache does: KServe resolves the credentials when it creates the
        # download Job, and does not revisit a Job it has already made.
        secret = self.download_secret_manifest(inference)
        if secret:
            objects.append(secret)
        objects.extend(self.model_cache_manifests(inference))
        kube_apply(
            inference.kubeconfig_path,
            objects,
            timeout=self.command_timeout,
            error_cls=InferenceError,
        )
        self._wait_for_model_cache(inference)
        self._reclaim_released_volume(inference)

    def _check_cache_source(self, inference: Inference) -> None:
        """Refuses a cache whose URI another object store already cached.

        KServe keeps one copy per URI on each node, whichever store it came
        from, and does not download again when that copy exists. So
        `s3://models/qwen/v1` from a second store would be served the
        first store's files without a word. Caches without the endpoint
        annotation predate it and cannot be told apart, so only a recorded
        mismatch is refused.
        """
        uri = self.storage_uri(inference)
        endpoint = inference.s3_endpoint_url or ""
        raw = self._kubectl(
            inference,
            "get",
            "localmodelcaches,localmodelnamespacecaches",
            "-A",
            "-o",
            "json",
            check=False,
        )
        try:
            items = json.loads(raw).get("items", []) if raw.strip() else []
        except ValueError:
            return
        for item in items:
            if item.get("spec", {}).get("sourceModelUri") != uri:
                continue
            recorded = (item.get("metadata", {}).get("annotations") or {}).get(
                CACHE_ENDPOINT_ANNOTATION
            )
            if recorded is not None and recorded != endpoint:
                where = item["metadata"].get("namespace") or "cluster scope"
                raise InferencePrerequisiteError(
                    f"{uri} is already cached from {recorded or 'no endpoint'} "
                    f"({item['kind']} {item['metadata']['name']}, {where}), and "
                    f"this deployment reads it from {endpoint or 'no endpoint'}. "
                    "KServe keeps one copy per URI on a node, so this "
                    "deployment would be served the other store's files. "
                    "Use a path unique to this store."
                )

    def download_secret_manifest(self, inference: Inference) -> dict[str, Any] | None:
        """This deployment's S3 Secret, copied into KServe's job namespace
        under the cache's download account name.

        The data is read from the cluster and written straight back to it;
        it is never printed or kept. Only KServe's own `serving.kserve.io/`
        annotations come along -- they carry the endpoint and region, and
        without them the download goes to AWS rather than the object store.
        """
        account = self.cache_service_account(inference)
        if not account:
            return None
        raw = self._kubectl(
            inference,
            "get",
            "secret",
            inference.s3_secret_name,
            "-n",
            inference.resolved_namespace,
            "-o",
            "json",
            check=False,
        )
        if not raw.strip():
            raise InferencePrerequisiteError(
                f"Secret '{inference.s3_secret_name}' is not in "
                f"{inference.resolved_namespace}, so the model cache's download "
                "has no credentials to copy. Create it there first."
            )
        source = json.loads(raw)
        annotations = {
            k: v
            for k, v in (source.get("metadata", {}).get("annotations") or {}).items()
            if k.startswith("serving.kserve.io/")
        }
        return {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": source.get("type", "Opaque"),
            "metadata": {
                "name": account,
                "namespace": inference.options.platform.model_cache.job_namespace,
                "labels": {CACHE_CREDENTIALS_LABEL: self.cache_name(inference)},
                "annotations": annotations,
            },
            "data": source.get("data", {}),
        }

    def _reclaim_released_volume(self, inference: Inference) -> None:
        """Frees this namespace's cache volume if KServe left it Released.

        When the last deployment of a model leaves a namespace, KServe
        deletes that namespace's claim on the cache but keeps the volume
        (reclaim policy Retain), still recording the deleted claim. The next
        deployment of the same model there gets a new claim of the same name
        that the volume refuses -- "already bound to a different claim" --
        and the pod never starts. Found on a live KServe 0.20 cluster on the
        first redeploy after a delete.

        The volume holds exactly this model's cache, so clearing the stale
        reference and letting the new claim bind is the right outcome. Only
        a Released volume whose recorded claim is this model's, in this
        namespace, is touched.
        """
        group = self.cache_node_group(inference)
        name = f"{self.cache_name(inference)}-{group}-{inference.resolved_namespace}"
        raw = self._kubectl(inference, "get", "pv", name, "-o", "json", check=False)
        if not raw.strip():
            return
        try:
            volume = json.loads(raw)
        except ValueError:
            return
        claim = volume.get("spec", {}).get("claimRef") or {}
        if (
            volume.get("status", {}).get("phase") == "Released"
            and claim.get("namespace") == inference.resolved_namespace
            and claim.get("name") == f"{self.cache_name(inference)}-{group}"
        ):
            self._kubectl(
                inference,
                "patch",
                "pv",
                name,
                "--type",
                "json",
                "-p",
                '[{"op": "remove", "path": "/spec/claimRef"}]',
            )
            print(f"[{self.log_prefix}] freed released cache volume {name} for reuse")

    def _wait_for_model_cache(self, inference: Inference) -> None:
        """Until every node in the cache's group reports NodeDownloaded.

        A NodeDownloadError stops it straight away with where to look: the
        download Job's logs, which is where a bad credential or an
        unwritable folder shows -- and which nothing else surfaces.
        """
        if inference.resolved_cache_scope == "namespace":
            kind = [
                "localmodelnamespacecache",
                self.cache_name(inference),
                "-n",
                inference.resolved_namespace,
            ]
        else:
            kind = ["localmodelcache", self.cache_name(inference)]
        timeout = inference.options.ready_timeout
        deadline = time.time() + timeout
        last: dict[str, str] = {}
        while time.time() < deadline:
            nodes = (
                self._status(
                    self._kubectl(
                        inference,
                        "get",
                        *kind,
                        "-o",
                        "json",
                        check=False,
                    )
                ).get("nodeStatus")
                or {}
            )
            if nodes and all(v == "NodeDownloaded" for v in nodes.values()):
                print(
                    f"[{self.log_prefix}] model cache {self.cache_name(inference)} "
                    f"downloaded on {', '.join(sorted(nodes))}"
                )
                return
            failed = sorted(n for n, v in nodes.items() if v == "NodeDownloadError")
            if failed:
                raise InferenceError(
                    f"KServe could not download {self.storage_uri(inference)} into "
                    f"the cache on {', '.join(failed)}. The download Job's logs say "
                    "why: `kubectl -n "
                    f"{inference.options.platform.model_cache.job_namespace} logs "
                    f"-l job-name --tail=20 --kubeconfig {inference.kubeconfig_path}`."
                )
            if nodes != last:
                print(
                    f"[{self.log_prefix}]   model cache: "
                    + ", ".join(f"{n}={v}" for n, v in sorted(nodes.items()))
                )
                last = dict(nodes)
            time.sleep(10)
        raise InferenceError(
            f"the model cache {self.cache_name(inference)} was not downloaded "
            f"within {timeout}s (last: {last or 'no node status yet'}). Check the "
            "LocalModelNodeGroup's nodes carry the agent's label and the agent "
            "is running there."
        )

    def _delete_model_cache(self, inference: Inference) -> None:
        """Removes this model's cache resource. Only on request: the cache
        belongs to the model, and another deployment may be using it."""
        if inference.model_cache != "node":
            return
        args = ["delete"]
        if inference.resolved_cache_scope == "namespace":
            args += [
                "localmodelnamespacecache",
                self.cache_name(inference),
                "-n",
                inference.resolved_namespace,
            ]
        else:
            args += ["localmodelcache", self.cache_name(inference)]
        self._kubectl(inference, *args, "--ignore-not-found")
        account = self.cache_service_account(inference)
        if account:
            self._kubectl(
                inference,
                "delete",
                "serviceaccount,secret",
                account,
                "-n",
                inference.options.platform.model_cache.job_namespace,
                "--ignore-not-found",
            )

    # -- manifests ---------------------------------------------------------
    def storage_uri(self, inference: Inference) -> str:
        """`spec.predictor.model.storageUri`.

        The translation this implementation needs and the others do not:
        KServe fetches weights itself, so an `s3://` model goes through
        unchanged and a HuggingFace repo id becomes `hf://<repo>`. There
        is no mirror init container anywhere in this driver as a result.
        """
        if inference.model_is_s3:
            return inference.model
        return f"hf://{inference.model}"

    def container_args(self, inference: Inference) -> list[str]:
        """Args for the predictor container.

        Deliberately narrow. KServe passes these to whichever
        ClusterServingRuntime it selected, and an argument that runtime
        does not know is a crash loop rather than a warning — so only the
        huggingface runtime's own spellings are emitted, only for fields
        the caller actually set. Anything else belongs in `extra_args`,
        where it is the caller's statement about their own runtime.
        """
        args: list[str] = []
        # Only when explicitly set: KServe otherwise derives the served
        # model name from the InferenceService name, and overriding it
        # with the spec's default would change what clients must send in
        # the `model` field for no reason the caller asked for.
        if inference.served_model_name:
            args += ["--model_name", inference.served_model_name]

        if inference.options.model_format == "huggingface":
            args += ["--max_model_len", str(inference.max_model_len)]
            if inference.dtype and inference.dtype != "auto":
                args += ["--dtype", inference.dtype]

        return args + list(inference.extra_args)

    def env(self, inference: Inference) -> list[dict[str, str]]:
        """Environment for the predictor container.

        The huggingface runtime is vLLM-backed, so on CPU the spec's
        vLLM tuning fields are read by the engine inside KServe's image
        exactly as they are inside the vllm driver's. On GPU, and under
        any other runtime, they mean nothing and are left off rather than
        set to a value nothing reads.
        """
        if inference.device != "cpu" or inference.options.model_format != "huggingface":
            return []
        return [{"name": key, "value": value} for key, value in sorted(inference.env().items())]

    def resources(self, inference: Inference) -> dict[str, dict[str, str]]:
        requests = {
            "cpu": str(inference.cpu_cores),
            "memory": f"{inference.memory_gb}Gi",
        }
        limits = dict(requests)
        if inference.device == "gpu":
            requests[inference.gpu_resource] = str(inference.gpu_count)
            limits[inference.gpu_resource] = str(inference.gpu_count)
        return {"requests": requests, "limits": limits}

    def manifests(self, inference: Inference) -> list[dict[str, Any]]:
        """Namespace, optional ServiceAccount, the InferenceService, and
        the optional NetworkPolicy/ServiceMonitor. Public so a caller can
        render and diff without touching a cluster, as the other two
        drivers allow."""
        options = inference.options
        namespace = inference.resolved_namespace
        selector = {ISVC_LABEL: inference.name}

        model: dict[str, Any] = {
            "modelFormat": {"name": options.model_format},
            "storageUri": self.storage_uri(inference),
            "resources": self.resources(inference),
        }
        if options.runtime:
            model["runtime"] = options.runtime

        args = self.container_args(inference)
        if args:
            model["args"] = args
        env = self.env(inference)
        if env:
            model["env"] = env

        predictor: dict[str, Any] = {"model": model}
        if options.min_replicas is not None:
            predictor["minReplicas"] = options.min_replicas
        if options.max_replicas is not None:
            predictor["maxReplicas"] = options.max_replicas
        if inference.node_selector:
            predictor["nodeSelector"] = dict(inference.node_selector)
        if inference.tolerations:
            predictor["tolerations"] = list(inference.tolerations)
        if inference.runtime_class:
            # Without this the container is handed the GPU as a scheduled
            # resource and still cannot see it: RKE2 ships the `nvidia`
            # RuntimeClass separately, and the CUDA devices and libraries
            # are mounted by that runtime, not by the device plugin.
            # Observed on a live RKE2 cluster -- the pod scheduled onto
            # the GPU node with nvidia.com/gpu=1 and then crash-looped on
            # "No platform detected, vLLM is running on UnspecifiedPlatform
            # / Failed to infer device type", which reads like an
            # unsupported card rather than a missing runtime.
            predictor["runtimeClassName"] = inference.runtime_class

        service_account = options.service_account_name
        if inference.model_is_s3 and not service_account:
            # KServe reads bucket credentials through the pod's
            # ServiceAccount, so an s3:// model needs one bound to the
            # Secret. Named after the deployment rather than reusing
            # `default`, which is shared with everything else in the
            # namespace.
            service_account = f"{inference.name}-storage"
        if service_account:
            predictor["serviceAccountName"] = service_account

        isvc: dict[str, Any] = {
            "apiVersion": ISVC_API_VERSION,
            "kind": "InferenceService",
            "metadata": {
                "name": inference.name,
                "namespace": namespace,
                "annotations": {
                    DEPLOYMENT_MODE_ANNOTATION: options.canonical_deployment_mode,
                },
            },
            "spec": {"predictor": predictor},
        }
        if options.canary_traffic_percent is not None:
            isvc["spec"]["predictor"]["canaryTrafficPercent"] = options.canary_traffic_percent

        objects: list[dict[str, Any]] = [
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": namespace},
            },
        ]
        if inference.model_is_s3 and not options.service_account_name:
            # Only when this driver named it. A caller who supplied
            # service_account_name owns that object, and creating one over
            # the top of theirs would drop whatever else it references.
            objects.append(
                {
                    "apiVersion": "v1",
                    "kind": "ServiceAccount",
                    "metadata": {"name": service_account, "namespace": namespace},
                    "secrets": [{"name": inference.s3_secret_name}],
                }
            )
        # Before the InferenceService, so the cache exists by the time
        # KServe looks for one matching its storageUri.
        objects.extend(self.model_cache_manifests(inference))
        objects.append(isvc)

        if inference.allowed_client_labels:
            objects.append(
                network_policy_manifest(
                    f"{inference.name}-allow-clients",
                    namespace,
                    selector,
                    allowed_ingress=inference.allowed_client_labels,
                    # The port the pod serves on, not the Service's: a
                    # NetworkPolicy matches pods directly, so the predictor
                    # Service's port 80 is not what arrives at the container.
                    # KServe's, not `Inference.port` -- see
                    # KServeOptions.predictor_container_port.
                    ports=[options.predictor_container_port],
                )
            )
        if inference.service_monitor_labels:
            objects.append(
                service_monitor_manifest(
                    f"{inference.name}-metrics",
                    namespace,
                    selector,
                    # KServe names the Service port after the Service, so
                    # this is `<name>-predictor` unless a caller says
                    # otherwise -- `inference.service_name` is that name.
                    port=options.metrics_port_name or inference.service_name,
                    extra_labels=dict(inference.service_monitor_labels),
                )
            )
        return objects

    # -- lifecycle ---------------------------------------------------------
    def create(self, inference: Inference, nodes: list | None = None) -> str:
        """Applies the InferenceService and returns the endpoint once
        KServe reports it Ready."""
        inference.validate()
        require_cli("kubectl", error_cls=InferencePrerequisiteError)
        require_cluster(inference.kubeconfig_path, capability="inference")

        # Before the checks, not after: several of them ask about APIs
        # that only exist once KServe does, so checking first would only
        # report the absence this is about to fix.
        for note in self.install_platform(inference):
            print(f"[{self.log_prefix}] {note}")

        for warning in self.check_prerequisites(inference, nodes):
            print(f"[{self.log_prefix}] warning: {warning}")

        self._apply_model_cache_first(inference)

        print(
            f"[{self.log_prefix}] applying {inference.name} to "
            f"namespace {inference.resolved_namespace}"
        )
        kube_apply(
            inference.kubeconfig_path,
            self.manifests(inference),
            timeout=self.command_timeout,
            error_cls=InferenceError,
        )

        self._wait_until_ready(inference)
        endpoint = self.endpoint(inference)

        if inference.warmup:
            print(f"[{self.log_prefix}] sending warmup request (first request is much slower)")
            self._warmup(inference)

        print(f"[{self.log_prefix}] serving at {endpoint}")
        return endpoint

    def delete(self, inference: Inference, remove_cache: bool = False) -> None:
        """Removes the deployment. Its model cache stays unless
        `remove_cache` -- see `_delete_model_cache`."""
        inference.validate()
        namespace = inference.resolved_namespace
        # The InferenceService first: KServe garbage-collects the
        # Deployment, Service and autoscaler it owns, so removing those
        # by hand would race its controller rather than help it.
        self._kubectl(
            inference,
            "delete",
            "inferenceservice",
            inference.name,
            "-n",
            namespace,
            "--ignore-not-found",
        )
        options = inference.options
        if inference.model_is_s3 and not options.service_account_name:
            self._kubectl(
                inference,
                "delete",
                "serviceaccount",
                f"{inference.name}-storage",
                "-n",
                namespace,
                "--ignore-not-found",
            )
        if inference.allowed_client_labels:
            self._kubectl(
                inference,
                "delete",
                "networkpolicy",
                f"{inference.name}-allow-clients",
                "-n",
                namespace,
                "--ignore-not-found",
            )
        # Not gated on `service_monitor_labels`, which it was until
        # 2026-10-09. The name is derived entirely from the spec, so a
        # caller tearing down by name has no reason to restate the
        # labels it was created with -- and gating on them meant a
        # teardown spec built the obvious way silently left the monitor
        # behind. check=False for a cluster with no Prometheus Operator:
        # there the resource type itself does not exist, which
        # --ignore-not-found does not cover.
        self._kubectl(
            inference,
            "delete",
            "servicemonitor",
            f"{inference.name}-metrics",
            "-n",
            namespace,
            "--ignore-not-found",
            check=False,
        )
        if remove_cache:
            self._delete_model_cache(inference)
        print(f"[{self.log_prefix}] removed {inference.name} from {namespace}")

    def endpoint(self, inference: Inference) -> str:
        """Where it actually serves.

        KServe publishes the cluster-internal address on the object, and
        that is the authority: it reflects the deployment mode, which
        decides whether the address is the predictor Service or a Knative
        route. The static form on the spec is the fallback for a cluster
        that has not populated status yet.
        """
        raw = self._kubectl(
            inference,
            "get",
            self.resource_kind,
            inference.name,
            "-n",
            inference.resolved_namespace,
            "-o",
            "json",
            check=False,
        )
        url = self._status(raw).get("address", {}).get("url", "")
        if not url:
            return inference.endpoint
        # KServe publishes the Service address, which is the host and
        # nothing else. Callers append `/v1/chat/completions` to whatever
        # this returns, so the runtime's own prefix has to be carried
        # here too -- see Inference.base_path.
        return url.rstrip("/") + inference.base_path

    # -- internals ---------------------------------------------------------
    @staticmethod
    def _status(raw: str) -> dict[str, Any]:
        """`.status` from a `kubectl get -o json`, or an empty mapping.

        Parsed rather than pulled out with a jsonpath. The query needed
        here selects a condition by type, which means quotes and a filter
        expression inside a Python string — and a jsonpath that silently
        matches nothing is this repo's most-repeated bug, reported last
        time as "no StorageClass at all" on a cluster with two.
        """
        if not raw.strip():
            return {}
        try:
            status = json.loads(raw).get("status", {})
        except ValueError:
            return {}
        return status if isinstance(status, dict) else {}

    @staticmethod
    def _condition(status: dict[str, Any], kind: str) -> dict[str, Any]:
        for condition in status.get("conditions", []) or []:
            if isinstance(condition, dict) and condition.get("type") == kind:
                return condition
        return {}

    def _wait_until_ready(self, inference: Inference) -> None:
        timeout = inference.options.ready_timeout
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            status = self._status(
                self._kubectl(
                    inference,
                    "get",
                    self.resource_kind,
                    inference.name,
                    "-n",
                    inference.resolved_namespace,
                    "-o",
                    "json",
                    check=False,
                )
            )
            ready = self._condition(status, "Ready")
            if ready.get("status") == "True":
                return
            if self._serving_without_an_address(status):
                print(
                    f"[{self.log_prefix}] warning: KServe will not report "
                    f"{inference.name} ready because the ingress gateway has "
                    "no external address, but the deployment is up and "
                    "serving — treating it as created. Give the cluster a "
                    "LoadBalancer provider (MetalLB or equivalent) to get a "
                    "real readiness signal."
                )
                return

            # Whatever KServe is currently unhappy about, printed once per
            # change. The Ready condition's own message is empty while it
            # is merely progressing, so the reason usually sits on
            # PredictorReady instead.
            detail = (
                ready.get("message")
                or self._condition(status, "PredictorReady").get("message")
                or ready.get("reason")
                # A rejected InferenceService has no conditions at all --
                # nothing reconciled it, so there is nothing to report
                # from status. The reason is in the events instead, and
                # that is exactly the case where it matters most.
                or self._latest_warning(inference)
                or ""
            )
            if detail and detail != last:
                print(f"[{self.log_prefix}]   {detail}")
                last = detail
            time.sleep(10)

        raise InferenceError(
            f"{inference.name} was not ready within {timeout}s"
            + (f" (last status: {last})" if last else "")
            + ". Check `kubectl -n "
            f"{inference.resolved_namespace} describe {self.resource_kind} "
            f"{inference.name} --kubeconfig {inference.kubeconfig_path}`; a "
            "first start also downloads the weights, so check the "
            "storage-initializer container's logs before raising the timeout."
        )

    @classmethod
    def _serving_without_an_address(cls, status: dict[str, Any]) -> bool:
        """Whether the only thing missing is an ingress address.

        On a cluster with no LoadBalancer provider, KServe holds a
        Serverless InferenceService at 'Waiting for load balancer to be
        ready' forever -- and that is not a failure mode, it is a
        reporting one. Observed live: the Knative route, ingress and
        revision all went Ready, a request through the real gateway
        returned 200 from the model, and KServe still reported Unknown
        until create() timed out and called a working deployment broken.

        So this is narrow on purpose. It requires the deployment itself
        to be up (`LatestDeploymentReady`), and the thing blocking Ready
        to be the address specifically. Anything else -- a crash loop, a
        missing image, a rejected pod -- leaves `LatestDeploymentReady`
        false and still times out, which is what should happen.
        """
        if cls._condition(status, "LatestDeploymentReady").get("status") != "True":
            return False
        blocking = " ".join(
            (cls._condition(status, kind).get("message") or "")
            for kind in ("Ready", "PredictorReady", "PredictorRouteReady")
        ).lower()
        return "load balancer" in blocking

    def _latest_warning(self, inference: Inference) -> str:
        """The most recent Warning event on this InferenceService.

        Status conditions are the right source while something is
        reconciling it. When an admission webhook *rejects* it there are
        no conditions at all, so a timeout would otherwise report only
        that time passed -- observed live against Knative, which refused
        a GPU runtime's hostPath volumes and left the deployment silent
        for the full timeout while the reason sat in the events.
        """
        raw = self._kubectl(
            inference,
            "get",
            "events",
            "-n",
            inference.resolved_namespace,
            "--field-selector",
            f"involvedObject.name={inference.name},type=Warning",
            "-o",
            "json",
            check=False,
        )
        if not raw.strip():
            return ""
        try:
            items = json.loads(raw).get("items", [])
        except ValueError:
            return ""
        if not items:
            return ""
        # Last by observed time, falling back to the ordering the API
        # returned when a timestamp is absent.
        latest = max(
            items,
            key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "",
        )
        return (latest.get("message") or "").strip()

    def _warmup(self, inference: Inference) -> None:
        """Best-effort: a failure here doesn't mean the service is broken."""
        options = inference.options
        # The prefix is already in the endpoint. Warmup used to add it
        # here instead, which meant this ping was the only thing in the
        # driver that reached the real route -- while the endpoint every
        # other consumer was handed pointed at one the runtime does not
        # serve.
        url = f"{self.endpoint(inference)}/v1/chat/completions"
        body = json.dumps(
            {
                # KServe serves under the InferenceService name unless
                # --model_name overrode it -- see container_args.
                "model": inference.served_model_name or inference.name,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            }
        ).encode()
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}
        )
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=options.warmup_timeout):
                print(f"[{self.log_prefix}] warmup completed in {time.time() - started:.0f}s")
        except (urllib.error.URLError, OSError) as exc:
            # A cluster-internal name does not resolve from outside the
            # pod network, and this SDK is meant to run from the first
            # control-plane node, which does not use cluster DNS. So this
            # is the normal outcome there, not a sign the service is
            # unhealthy -- saying "the first request will be slow" implied
            # the warmup had been attempted and the service was merely
            # cold. It had not been attempted at all.
            if self._is_name_resolution_error(exc):
                print(
                    f"[{self.log_prefix}] warmup skipped: "
                    f"{inference.service_name} is a cluster-internal name "
                    "and does not resolve from here. Warmup only runs from "
                    "inside the cluster; the first real request will be "
                    "slow, as it would be with no warmup configured."
                )
                return
            print(
                f"[{self.log_prefix}] warning: warmup request failed ({exc}) — "
                "the service is ready but the first real request will be slow."
            )

    @staticmethod
    def _is_name_resolution_error(exc: BaseException) -> bool:
        """Whether a warmup failure was DNS rather than the service.

        Checked by message because the exception is a URLError wrapping a
        socket.gaierror whose errno varies by platform -- what is stable
        is that the name could not be resolved.
        """
        text = str(exc).lower()
        return any(
            marker in text
            for marker in (
                "name or service not known",
                "nodename nor servname",
                "temporary failure in name resolution",
                "name resolution",
                "getaddrinfo",
            )
        )

    def _exists(self, inference: Inference, kind: str, name: str) -> bool:
        """Whether a cluster-scoped object is present. `-o name` with
        check=False, so an absent object is empty output rather than a
        raised error -- which is the question being asked."""
        return bool(
            self._kubectl(
                inference,
                "get",
                kind,
                name,
                "-o",
                "name",
                check=False,
            ).strip()
        )

    def _kubectl(self, inference: Inference, *args: str, check: bool = True) -> str:
        return kube_kubectl(
            inference.kubeconfig_path,
            *args,
            check=check,
            timeout=self.command_timeout,
            error_cls=InferenceError,
        )
