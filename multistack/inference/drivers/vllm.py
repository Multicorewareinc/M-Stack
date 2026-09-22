"""
The vLLM implementation: renders an `Inference` spec into Kubernetes
objects, applies them with `kubectl`, waits for the endpoint to serve, and
optionally warms it.

Two halves, as with the Longhorn/RKE2 backends:

  * **Cluster-side** — deployment/service via `kubectl --kubeconfig`, never
    ambient kubeconfig resolution.
  * **Node-side** (optional) — probes the target node's CPU flags over SSH
    so the dtype can be chosen from what the hardware actually supports.
"""
from __future__ import annotations
import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from ...backends.transport import NodeCommandMixin
from ...kube import apply as kube_apply
from ...kube import kubectl as kube_kubectl
from ...kube import require_cli, require_cluster
from ...netpolicy import network_policy_manifest
from ...servicemonitor import service_monitor_manifest
from ..base import InferenceError, InferencePrerequisiteError
from ..spec import DTYPE_CPU_FLAGS, LOCAL_MODEL_DIR, Inference


class VLLMDriver(NodeCommandMixin):
    """Deploys and removes vLLM-backed inference services."""

    error_cls = InferenceError
    prerequisite_error_cls = InferencePrerequisiteError
    log_prefix = "inference/vllm"

    def __init__(
        self,
        ssh_timeout: int = 30,
        command_timeout: int = 300,
        max_parallel: int = 10,
    ):
        self.ssh_timeout = ssh_timeout
        self.command_timeout = command_timeout
        self.max_parallel = max_parallel

    # -- node-side checks ---------------------------------------------------
    def recommended_dtype(self, node) -> str:
        """Picks a dtype from what `node`'s CPU can do natively, rather
        than letting vLLM take it from the model config and silently
        emulate an unsupported dtype in software."""
        flags = self._probe(node, "cat /proc/cpuinfo", "CPU flags")
        for dtype in ("bfloat16", "float16"):
            if any(flag in flags for flag in DTYPE_CPU_FLAGS[dtype]):
                return dtype
        return "float32"

    def check_prerequisites(self, inference: Inference, nodes: Optional[List] = None) -> List[str]:
        """Checks the target nodes can host this service. Pass the nodes
        it's pinned to (matching node_selector), not the whole cluster."""
        warnings: List[str] = []
        for node in nodes or []:
            warnings.extend(self._check_node(node, inference))
        return warnings

    def _check_node(self, node, inference: Inference) -> List[str]:
        warnings: List[str] = []

        cores = self._parse_int(self._probe(node, "nproc", "CPU core count"))
        if cores is not None and inference.cpu_cores > cores:
            raise InferencePrerequisiteError(
                f"{node.address}: service asks for {inference.cpu_cores} cores "
                f"but the node has {cores}. The pod would stay Pending with "
                "'Insufficient cpu'."
            )
        if cores is not None and inference.cpu_cores == cores:
            warnings.append(
                f"{node.address}: requesting all {cores} cores leaves nothing "
                "for kubelet, the CNI and other DaemonSets — the pod may not "
                "schedule. Leave 1-2 cores free."
            )

        ram_mb = self._parse_int(
            self._probe(node, "free -m | awk '/^Mem:/{print $2}'", "total RAM")
        )
        if ram_mb is not None and inference.memory_gb * 1024 > ram_mb:
            raise InferencePrerequisiteError(
                f"{node.address}: service asks for {inference.memory_gb}GB but "
                f"the node has {ram_mb}MB total."
            )

        if inference.device == "cpu" and inference.dtype is None:
            if self.recommended_dtype(node) == "float32":
                warnings.append(
                    f"{node.address}: no native bf16/fp16 support, so dtype "
                    "float32 will be used."
                )

        if self._probe(
            node,
            "test -e /var/lib/rancher/rke2/server/db && echo server || echo agent",
            "node role",
        ) == "server":
            warnings.append(
                f"{node.address}: this is a control-plane/etcd node. Inference "
                "here contends with etcd and the API server — pin the service "
                "to a worker instead."
            )

        return warnings

    # -- cluster-side -------------------------------------------------------
    def create(self, inference: Inference, nodes: Optional[List] = None) -> str:
        """Deploys (or updates) the service and returns its base URL once
        it is serving. With `nodes`, prerequisites are checked and an
        unset dtype is chosen from their CPU flags."""
        inference.validate()
        require_cluster(inference.kubeconfig_path, capability="inference")
        self._require_cli("kubectl")

        dtype = inference.dtype
        if nodes:
            print(f"[inference/vllm] checking prerequisites on {len(nodes)} node(s)")
            for w in self.check_prerequisites(inference, nodes):
                print(f"[inference/vllm] warning: {w}")
            if dtype is None and inference.device == "cpu":
                dtype = self.recommended_dtype(nodes[0])
                print(f"[inference/vllm] selected dtype={dtype} from node CPU flags")

        print(f"[inference/vllm] applying {inference.name} to "
              f"namespace {inference.resolved_namespace}")
        self._kubectl_apply(inference, self.manifests(inference, dtype=dtype))

        self._wait_until_ready(inference)
        endpoint = self.endpoint(inference)

        if inference.warmup:
            print("[inference/vllm] sending warmup request (first request is much slower)")
            self._warmup(inference)

        print(f"[inference/vllm] serving at {endpoint}")
        return endpoint

    def delete(self, inference: Inference) -> None:
        inference.validate()
        self._require_cli("kubectl")
        for kind in ("deployment", "service"):
            self._kubectl(
                inference, "delete", kind, inference.name,
                "-n", inference.resolved_namespace, "--ignore-not-found",
            )
        print(f"[inference/vllm] removed {inference.name} from {inference.resolved_namespace}")

    def endpoint(self, inference: Inference) -> str:
        """With `host_network`, the pod answers on its node's address, so
        the (live) node IP is returned; otherwise the static in-cluster
        endpoint on the spec is already correct."""
        if inference.host_network:
            host = self._kubectl(
                inference, "get", "pods",
                "-n", inference.resolved_namespace,
                "-l", f"app={inference.name}",
                "-o", "jsonpath={.items[0].status.hostIP}",
            ).strip()
            return f"http://{host}:{inference.port}"
        return inference.endpoint

    # -- manifest rendering -------------------------------------------------
    def manifests(self, inference: Inference, dtype: Optional[str] = None) -> List[Dict[str, Any]]:
        """Renders the Namespace, Deployment and Service. Public so callers
        can inspect/diff what would be applied without touching a cluster."""
        namespace = inference.resolved_namespace
        env = [{"name": k, "value": v} for k, v in sorted(inference.env().items())]

        # Credentials for mirror_command()'s `mc` (MinIO Client CLI)
        # `alias set` — read from the existing s3_secret_name Secret, never
        # generated or written here.
        mirror_env = [
            {
                "name": key,
                "valueFrom": {
                    "secretKeyRef": {"name": inference.s3_secret_name, "key": key}
                },
            }
            for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
        ] if inference.s3_secret_name else []

        resources: Dict[str, Dict[str, str]] = {
            "requests": {
                "cpu": str(inference.cpu_cores),
                "memory": f"{inference.memory_gb}Gi",
            },
            "limits": {
                "cpu": str(inference.cpu_cores),
                "memory": f"{inference.memory_gb}Gi",
            },
        }
        if inference.device == "gpu":
            resources["requests"]["nvidia.com/gpu"] = str(inference.gpu_count)
            resources["limits"]["nvidia.com/gpu"] = str(inference.gpu_count)

        if inference.model_is_s3:
            model_mounts = [{"name": "model-store", "mountPath": LOCAL_MODEL_DIR}]
        else:
            model_mounts = [
                {"name": "model-store", "mountPath": "/root/.cache/huggingface"}
            ]

        pod_spec: Dict[str, Any] = {
            # Kubernetes otherwise injects a `VLLM_PORT=tcp://<clusterIP>:...`
            # env var into every pod in this namespace once the `vllm`
            # Service exists (Docker-links-style discovery vars, named
            # after the Service). vLLM's own config reads VLLM_PORT as its
            # own setting and chokes on that URI value -- a same-name
            # collision that crash-loops the pod. Confirmed live: the
            # default name ("vllm") hits this on every deploy.
            "enableServiceLinks": False,
            "containers": [{
                "name": "vllm",
                "image": inference.container_image,
                "args": inference.container_args(dtype=dtype),
                "env": env,
                "ports": [{"containerPort": inference.port, "name": "http"}],
                "resources": resources,
                "readinessProbe": {
                    "httpGet": {"path": "/health", "port": inference.port},
                    "initialDelaySeconds": 60,
                    "periodSeconds": 15,
                    "failureThreshold": 60,
                },
                "volumeMounts": model_mounts + [
                    {"name": "dshm", "mountPath": "/dev/shm"},
                ],
            }],
            "volumes": [
                {"name": "model-store", "emptyDir": {"sizeLimit": "20Gi"}},
                {
                    "name": "dshm",
                    "emptyDir": {"medium": "Memory", "sizeLimit": f"{inference.shm_gb}Gi"},
                },
            ],
        }
        if inference.model_is_s3:
            # Init container mirrors weights from the existing bucket to
            # local disk before the vLLM container starts — see
            # Inference.mirror_command().
            # No `if inference.options else <literal>` fallback here.
            # resolve_options() fills options in during construction, so
            # the branch was unreachable -- and the literal it carried was
            # a second copy of the default, which is how it kept
            # `minio/mc:latest` after the real default had moved.
            pod_spec["initContainers"] = [{
                "name": "fetch-weights",
                "image": inference.options.s3_mirror_image,
                "command": inference.mirror_command(),
                "env": mirror_env,
                "volumeMounts": model_mounts,
            }]

        if inference.runtime_class:
            pod_spec["runtimeClassName"] = inference.runtime_class
        if inference.node_selector:
            pod_spec["nodeSelector"] = dict(inference.node_selector)
        if inference.tolerations:
            pod_spec["tolerations"] = list(inference.tolerations)
        if inference.host_network:
            pod_spec["hostNetwork"] = True
            pod_spec["dnsPolicy"] = "ClusterFirstWithHostNet"

        deployment: Dict[str, Any] = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": inference.name, "namespace": namespace},
            "spec": {
                "replicas": inference.replicas,
                "selector": {"matchLabels": {"app": inference.name}},
                "template": {
                    "metadata": {"labels": {"app": inference.name}},
                    "spec": pod_spec,
                },
            },
        }
        if inference.host_network:
            deployment["spec"]["strategy"] = {"type": "Recreate"}

        objects: List[Dict[str, Any]] = [
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}},
            deployment,
            {
                "apiVersion": "v1",
                "kind": "Service",
                # `labels` here, not just `spec.selector` below -- a
                # ServiceMonitor's own selector matches a Service's own
                # metadata labels, which `spec.selector` is not: that
                # field is how this Service finds its own pods, and says
                # nothing about the Service object itself. Without this,
                # the ServiceMonitor below is created successfully and
                # Prometheus discovers it, but every candidate target it
                # finds gets silently relabeled into `droppedTargets`,
                # never `activeTargets` -- no error anywhere, just no
                # metrics (confirmed against a live cluster).
                "metadata": {
                    "name": inference.name,
                    "namespace": namespace,
                    "labels": {"app": inference.name},
                },
                "spec": {
                    "selector": {"app": inference.name},
                    "ports": [{
                        "port": inference.port,
                        "targetPort": inference.port,
                        "name": "http",
                    }],
                },
            },
        ]
        if inference.allowed_client_labels:
            objects.append(network_policy_manifest(
                f"{inference.name}-allow-clients",
                namespace,
                {"app": inference.name},
                allowed_ingress=inference.allowed_client_labels,
                ports=[inference.port],
            ))
        if inference.service_monitor_labels:
            # The Service above names its port "http" — ServiceMonitor
            # endpoints resolve by Service port *name*, not number.
            objects.append(service_monitor_manifest(
                f"{inference.name}-metrics",
                namespace,
                {"app": inference.name},
                port="http",
                extra_labels=dict(inference.service_monitor_labels),
            ))
        return objects

    # -- waiting and warmup -------------------------------------------------
    def _wait_until_ready(self, inference: Inference) -> None:
        timeout = inference.options.ready_timeout if inference.options else 1800
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            ready = self._parse_int(
                self._kubectl(
                    inference, "get", "deployment", inference.name,
                    "-n", inference.resolved_namespace,
                    "-o", "jsonpath={.status.readyReplicas}",
                    check=False,
                ) or "0"
            )
            if ready and ready >= 1:
                return

            status = self._kubectl(
                inference, "get", "pods",
                "-n", inference.resolved_namespace,
                "-l", f"app={inference.name}",
                "-o", "jsonpath={range .items[*]}{.metadata.name} "
                      "{.status.phase} restarts={.status.containerStatuses[0].restartCount}{'\\n'}{end}",
                check=False,
            ).strip()
            if status and status != last:
                print(f"[inference/vllm]   {status.splitlines()[0]}")
                last = status
            time.sleep(10)

        raise InferenceError(
            f"{inference.name} was not ready within {timeout}s. Check "
            f"`kubectl -n {inference.resolved_namespace} logs -l app={inference.name} "
            f"--kubeconfig {inference.kubeconfig_path}`."
        )

    def _warmup(self, inference: Inference) -> None:
        """Best-effort: a failure here doesn't mean the service is broken."""
        timeout = inference.options.warmup_timeout if inference.options else 600
        url = f"{self.endpoint(inference)}/v1/chat/completions"
        body = json.dumps({
            "model": inference.served_name,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }).encode()
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}
        )
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=timeout):
                print(f"[inference/vllm] warmup completed in {time.time() - started:.0f}s")
        except (urllib.error.URLError, OSError) as exc:
            print(
                f"[inference/vllm] warning: warmup request failed ({exc}) — the "
                "service is ready but the first real request will be slow."
            )

    # -- local CLI plumbing -------------------------------------------------
    def _require_cli(self, name: str) -> None:
        require_cli(name, error_cls=InferenceError, purpose="this driver drives it directly")

    def _kubectl_apply(self, inference: Inference, objects: List[Dict[str, Any]]) -> None:
        kube_apply(
            inference.kubeconfig_path, objects,
            timeout=self.command_timeout, error_cls=InferenceError,
        )

    def _kubectl(self, inference: Inference, *args: str, check: bool = True) -> str:
        return kube_kubectl(
            inference.kubeconfig_path, *args,
            check=check, timeout=self.command_timeout, error_cls=InferenceError,
        )
