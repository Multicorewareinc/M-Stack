"""
The llama.cpp implementation: quantised GGUF weights on a GPU that vLLM
cannot use.

Why it exists next to the vllm driver, since "two inference runtimes" is
not obviously worth the surface: the platform's card is compute
capability 6.1, below the 7.0 floor of every PyTorch build vLLM ships,
so on this cluster vLLM on the GPU fails with "no kernel image is
available for execution on the device". llama.cpp has its own CUDA
kernels and runs 4-bit weights, so a 7B model fits an 11GB card. This is
the deployment the agentic layer tests against.

Simpler than the vllm driver in one way and not in another: there is no
dtype to choose from the host's CPU flags, and no S3 mirror -- llama.cpp
fetches the GGUF itself from a `-hf` reference. What it does need is a
node-local cache, because that fetch is several GB per model.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from ...kube import apply as kube_apply
from ...kube import kubectl as kube_kubectl
from ...kube import require_cli, require_cluster
from ..base import InferenceError, InferencePrerequisiteError
from ..spec import Inference

# Where the GGUF cache is mounted inside the container. llama.cpp's `-hf`
# path uses the HuggingFace cache layout, so this is its variable's
# default rather than a choice.
CACHE_MOUNT_PATH = "/root/.cache/huggingface"
NVIDIA_GPU_RESOURCE = "nvidia.com/gpu"


class LlamaCppDriver:
    """Deploys and removes llama.cpp-backed inference services."""

    error_cls = InferenceError
    prerequisite_error_cls = InferencePrerequisiteError
    log_prefix = "inference/llamacpp"

    def __init__(self, command_timeout: int = 300) -> None:
        self.command_timeout = command_timeout

    # -- checks ------------------------------------------------------------
    def check_prerequisites(
        self, inference: Inference, nodes: Optional[List] = None
    ) -> List[str]:
        warnings: List[str] = []
        require_cli("kubectl", error_cls=InferencePrerequisiteError)
        require_cluster(inference.kubeconfig_path, capability="inference")

        if inference.device == "gpu":
            # The extended resource has to be advertised by something --
            # the `accelerator` capability, or an operator installed
            # outside this SDK. Without it the pod is unschedulable and
            # the event says only "Insufficient nvidia.com/gpu", which
            # does not hint at what to install.
            allocatable = kube_kubectl(
                inference.kubeconfig_path, "get", "nodes",
                "-o", "jsonpath={range .items[*]}"
                      "{.status.allocatable.nvidia\\.com/gpu} {end}",
                check=False, error_cls=InferenceError,
            )
            if not any(part.strip() for part in (allocatable or "").split()):
                raise InferencePrerequisiteError(
                    f"no node advertises {NVIDIA_GPU_RESOURCE}, so a pod "
                    "requesting one stays Pending with only 'Insufficient "
                    f"{NVIDIA_GPU_RESOURCE}' to explain it. Install the "
                    "device plugin first -- multistack.accelerator does "
                    "it, or an NVIDIA GPU Operator outside this SDK."
                )
            if inference.gpu_count < 1:
                warnings.append(
                    "device='gpu' with gpu_count=0 requests no card, so "
                    "llama.cpp will run every layer on the CPU regardless "
                    "of n_gpu_layers."
                )

        if not inference.node_selector:
            warnings.append(
                "no node_selector: the GGUF cache is a hostPath, so a pod "
                "rescheduled onto another node re-downloads the weights "
                "rather than finding them. Pin this to the machine with "
                "the card."
            )
        return warnings

    # -- manifests ---------------------------------------------------------
    def container_args(self, inference: Inference) -> List[str]:
        """llama.cpp's own flags, from the spec's model-request fields.

        Built here rather than on the spec -- unlike vLLM's, these are not
        a second spelling of a shared request shape: `-hf`, `-ngl` and
        `--cache-type-k` have no vLLM equivalent, and `container_args` on
        the spec is vllm's.
        """
        options = inference.options
        args = [
            # -hf, not --model: llama.cpp resolves a HuggingFace
            # repo:quant reference and downloads the GGUF itself, which is
            # why this driver needs no S3 mirror step.
            "-hf", inference.model,
            "-ngl", str(options.n_gpu_layers if inference.device == "gpu" else 0),
            "--host", "0.0.0.0",
            "--port", str(inference.port),
            # The spec's own field: llama.cpp calls the context window -c,
            # vLLM calls it --max-model-len, and they mean the same thing.
            "-c", str(inference.max_model_len),
        ]
        if options.cache_type_k:
            args += ["--cache-type-k", options.cache_type_k]
        args += ["-ub", str(options.ubatch_size), "-b", str(options.batch_size)]
        return args

    def manifests(self, inference: Inference) -> List[Dict[str, Any]]:
        """Namespace, Deployment and Service. Public so a caller can
        render without applying, the same way the vllm driver allows."""
        options = inference.options
        namespace = inference.resolved_namespace
        labels = {"app": inference.name}

        container: Dict[str, Any] = {
            "name": "server",
            "image": options.image,
            "args": self.container_args(inference),
            "ports": [{"containerPort": inference.port}],
            "volumeMounts": [
                {"name": "model-cache", "mountPath": CACHE_MOUNT_PATH}
            ],
            # No liveness probe on purpose: a cold start loads several GB
            # onto the card, and a liveness probe that fires during it
            # restarts the pod forever. Readiness gates traffic, which is
            # the part that matters.
            "readinessProbe": {
                "httpGet": {"path": "/health", "port": inference.port},
                "initialDelaySeconds": 15,
                "periodSeconds": 10,
                # 10 minutes of grace. The first start also downloads.
                "failureThreshold": 60,
            },
        }
        if inference.device == "gpu" and inference.gpu_count:
            container["resources"] = {
                "limits": {NVIDIA_GPU_RESOURCE: str(inference.gpu_count)}
            }

        pod_spec: Dict[str, Any] = {
            "containers": [container],
            "volumes": [{
                "name": "model-cache",
                "hostPath": {
                    "path": options.model_cache_path,
                    "type": "DirectoryOrCreate",
                },
            }],
        }
        if options.runtime_class_name:
            pod_spec["runtimeClassName"] = options.runtime_class_name
        if inference.node_selector:
            pod_spec["nodeSelector"] = dict(inference.node_selector)
        if inference.tolerations:
            pod_spec["tolerations"] = list(inference.tolerations)

        return [
            {
                "apiVersion": "v1", "kind": "Namespace",
                "metadata": {"name": namespace},
            },
            {
                "apiVersion": "apps/v1", "kind": "Deployment",
                "metadata": {"name": inference.name, "namespace": namespace},
                "spec": {
                    "replicas": inference.replicas,
                    "selector": {"matchLabels": labels},
                    # Recreate, not RollingUpdate: one card, and a second
                    # pod cannot have it. A rolling update would deadlock
                    # with the new pod Pending on the GPU the old one
                    # still holds.
                    "strategy": {"type": "Recreate"},
                    "template": {
                        "metadata": {"labels": labels},
                        "spec": pod_spec,
                    },
                },
            },
            {
                "apiVersion": "v1", "kind": "Service",
                "metadata": {"name": inference.name, "namespace": namespace},
                "spec": {
                    "type": options.service_type,
                    "selector": labels,
                    "ports": [dict(
                        # Named because a ServiceMonitor resolves a port
                        # by name, not number -- the one addition here
                        # that the live object does not have.
                        name="http",
                        port=inference.port,
                        targetPort=inference.port,
                        **({"nodePort": options.node_port}
                           if options.node_port else {}),
                    )],
                },
            },
        ]

    # -- lifecycle ---------------------------------------------------------
    def create(self, inference: Inference, nodes: Optional[List] = None) -> str:
        inference.validate()
        for warning in self.check_prerequisites(inference, nodes):
            print(f"[{self.log_prefix}] warning: {warning}")

        kube_apply(
            inference.kubeconfig_path, self.manifests(inference),
            timeout=self.command_timeout, error_cls=InferenceError,
        )
        self._wait_until_ready(inference)
        return inference.endpoint

    def delete(self, inference: Inference) -> None:
        namespace = inference.resolved_namespace
        for kind in ("service", "deployment"):
            kube_kubectl(
                inference.kubeconfig_path, "delete", kind, inference.name,
                "-n", namespace, "--ignore-not-found",
                timeout=self.command_timeout, error_cls=InferenceError,
            )
        # The hostPath cache is left alone. It holds gigabytes that cost
        # an hour to fetch, it is not this deployment's to own, and the
        # next install finds it -- which is the whole reason it is a
        # hostPath rather than an emptyDir.

    def endpoint(self, inference: Inference) -> str:
        return inference.endpoint

    # -- internals ---------------------------------------------------------
    def _wait_until_ready(self, inference: Inference) -> None:
        options = inference.options
        deadline = time.time() + (options.ready_timeout if options else 1800)
        while time.time() < deadline:
            ready = kube_kubectl(
                inference.kubeconfig_path, "get", "deployment", inference.name,
                "-n", inference.resolved_namespace,
                "-o", "jsonpath={.status.readyReplicas}",
                check=False, timeout=self.command_timeout,
                error_cls=InferenceError,
            )
            if (ready or "").strip() not in ("", "0"):
                return
            time.sleep(10)
        raise InferenceError(
            f"{inference.name} did not become ready within "
            f"{options.ready_timeout if options else 1800}s. A first start "
            "downloads the GGUF before it loads it, so check the pod's logs "
            "for download progress before raising the timeout."
        )
