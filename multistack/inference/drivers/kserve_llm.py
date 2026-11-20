"""
The kserve_llm implementation: hands an `LLMInferenceService` to KServe.

Built on the kserve driver rather than beside it. Both deploy onto the same
KServe, check the same GPU and bucket prerequisites and read readiness off
the same status conditions; what differs is the resource, what it needs
installed, and how the model server is described. So this subclasses
`KServeDriver` and replaces exactly those parts.

What an `LLMInferenceService` is, in the terms this file uses:

  * **No ServingRuntime.** KServe merges this resource's pod template over
    its own preset, `kserve-config-llm-template`, whose container -- named
    `main` -- runs `vllm serve /mnt/models` on port 8000. Our image (the
    ROCm build, for the AMD box) goes on that container; our flags are
    appended to the preset's command through `args`.
  * **Weights.** The storage-initializer fills /mnt/models from `model.uri`
    as before, or KServe mounts a LocalModelCache there when one matches
    the URI. For model_cache="stream" the storage-initializer is switched
    off (`spec.storageInitializer.enabled: false`) and the container's
    command is replaced, since the preset's hardcoded /mnt/models would
    otherwise be an empty folder.
  * **Exposure.** A workload Service, `<name>-kserve-workload-svc`, always;
    optionally a Gateway API route and the Inference Extension's endpoint
    picker in front of it.

Field names here are from KServe v0.20.0's own types
(pkg/apis/serving/v1alpha2/llm_inference_service_types.go) and preset
(charts/kserve-runtime-configs), not from a live cluster yet. The places
that most need checking on one are marked "unverified".
"""

from __future__ import annotations

import json
from typing import Any

from ...kube import apply as kube_apply
from ...kube import require_cli, require_cluster
from ...netpolicy import network_policy_manifest
from ..base import InferenceError, InferencePrerequisiteError
from ..spec import STANDARD_DEPLOYMENT_MODE, Inference
from .kserve import KServeDriver
from .kserve_platform import (
    GATEWAY_API_CRD,
    ISVC_CONFIGMAP,
    LLM_TEMPLATE_CONFIG,
    LLMISVC_CRD,
    KServePlatformInstaller,
)

LLMISVC_API_VERSION = "serving.kserve.io/v1alpha2"

# The preset's model-server container. Our template entry has to carry the
# same name, or KServe adds a second container instead of merging into it.
MAIN_CONTAINER = "main"

# Put on the model pods through `spec.labels`, so a NetworkPolicy can select
# them by a label this SDK set rather than one KServe chose.
POD_LABEL = "multistack.io/inference"

# Where KServe's self-signed serving certificate is mounted in the preset.
TLS_CERT = "/var/run/kserve/tls/tls.crt"
TLS_KEY = "/var/run/kserve/tls/tls.key"


class KServeLLMDriver(KServeDriver):
    """Deploys and removes LLMInferenceService-backed inference services."""

    log_prefix = "inference/kserve_llm"
    resource_kind = "llminferenceservice"

    # -- checks ------------------------------------------------------------
    def check_prerequisites(self, inference: Inference, nodes: list | None = None) -> list[str]:
        warnings: list[str] = []
        require_cli("kubectl", error_cls=InferencePrerequisiteError)
        require_cluster(inference.kubeconfig_path, capability="inference")
        options = inference.options
        installs = options.platform.install

        if not self._exists(inference, "crd", LLMISVC_CRD):
            if not installs:
                raise InferencePrerequisiteError(
                    f"this cluster has no {LLMISVC_CRD} CRD, so "
                    "LLMInferenceService is not installed. platform.install "
                    "is False, so this driver will not install it -- install "
                    "KServe's kserve-llmisvc-crd and kserve-llmisvc-resources "
                    "charts by hand, or set KServePlatform(install=True)."
                )
            return [
                *warnings,
                "LLMInferenceService is not installed yet; create() will "
                "install it, with the Gateway API CRDs and KServe's presets.",
            ]

        if not self._kubectl(
            inference,
            "get",
            "llminferenceserviceconfig",
            LLM_TEMPLATE_CONFIG,
            "-n",
            options.platform.namespace,
            "-o",
            "name",
            check=False,
        ).strip():
            if not installs:
                raise InferencePrerequisiteError(
                    f"LLMInferenceServiceConfig {LLM_TEMPLATE_CONFIG!r} is "
                    f"missing from {options.platform.namespace}, and every "
                    "LLMInferenceService is merged over it -- without it there "
                    "is no model-server container to run. Install "
                    "kserve-runtime-configs with kserve.llmisvcConfigs.enabled"
                    "=true, or set KServePlatform(install=True)."
                )
            warnings.append(
                "KServe's LLM presets are not installed yet; create() will install them."
            )

        if options.router == "gateway":
            warnings.extend(self._check_gateway(inference))

        warnings.extend(self._check_tls(inference))

        if inference.device == "gpu":
            warnings.extend(self._check_gpu_resource(inference))
            warnings.extend(self._check_gpu_runtime_class(inference))

        if inference.model_cache == "stream":
            if inference.image is None:
                warnings.append(
                    "model_cache='stream' with KServe's preset image: whether "
                    "it carries the Run:ai streamer (`vllm[runai]`) is "
                    "unverified. Set `image` to a build known to include it."
                )
        elif inference.model_is_s3:
            # The storage-initializer reads the bucket through the Secret's
            # KServe annotations, exactly as for an InferenceService.
            warnings.extend(self._check_s3_secret(inference))

        if inference.model_cache == "node":
            warnings.extend(self._check_model_cache(inference))
        return warnings

    def _check_gateway(self, inference: Inference) -> list[str]:
        options = inference.options
        if not self._exists(inference, "crd", GATEWAY_API_CRD):
            if not options.platform.install or not options.platform.gateway_api_version:
                raise InferencePrerequisiteError(
                    "router='gateway' needs the Gateway API, but this cluster "
                    f"has no {GATEWAY_API_CRD} CRD and this driver is not set "
                    "to install it. Apply the Gateway API standard-install "
                    "manifest, or use router='none'."
                )
            return []
        if options.gateway_name:
            namespace = options.gateway_namespace or inference.resolved_namespace
            if not self._kubectl(
                inference,
                "get",
                "gateway",
                options.gateway_name,
                "-n",
                namespace,
                "-o",
                "name",
                check=False,
            ).strip():
                raise InferencePrerequisiteError(
                    f"no Gateway {namespace}/{options.gateway_name} exists, so "
                    "the route KServe creates would attach to nothing."
                )
        return []

    def _check_tls(self, inference: Inference) -> list[str]:
        """Whether `options.tls` agrees with the cluster.

        KServe decides TLS for every LLMInferenceService from one setting in
        inferenceservice-config. When it is on, the preset serves HTTPS and
        probes over HTTPS; this driver has to know, because it writes the
        endpoint's scheme and, in stream mode, the serve command itself.
        """
        raw = self._kubectl(
            inference,
            "get",
            "configmap",
            ISVC_CONFIGMAP,
            "-n",
            inference.options.platform.namespace,
            "-o",
            "jsonpath={.data.ingress}",
            check=False,
        )
        if not raw.strip():
            return []
        try:
            cluster_tls = bool(json.loads(raw).get("enableLLMInferenceServiceTLS"))
        except ValueError:
            return []
        if cluster_tls == inference.options.tls:
            return []
        return [
            f"KServe's enableLLMInferenceServiceTLS is {cluster_tls} but "
            f"KServeLLMOptions.tls is {inference.options.tls}. The endpoint "
            "this driver publishes would use the wrong scheme"
            + (
                ", and the stream-mode server would not match the probes"
                if inference.model_cache == "stream"
                else ""
            )
            + f". Set tls={cluster_tls}."
        ]

    # -- the platform underneath -------------------------------------------
    def install_platform(self, inference: Inference) -> list[str]:
        """KServe with LLMInferenceService. Standard mode: an
        LLMInferenceService never routes through Knative."""
        installer = KServePlatformInstaller(
            inference.kubeconfig_path,
            command_timeout=self.command_timeout,
        )
        return installer.ensure(
            inference.options.platform,
            STANDARD_DEPLOYMENT_MODE,
            llm=True,
            model_cache=inference.model_cache == "node",
        )

    # -- manifests ---------------------------------------------------------
    def served_name(self, inference: Inference) -> str:
        """What clients send as `model`: KServe's own default (the resource
        name) unless the spec names one."""
        return inference.served_model_name or inference.name

    def container_args(self, inference: Inference) -> list[str]:
        """Flags appended to the preset's `vllm serve` command.

        The preset ends in `$@`, so these land after its own flags. Only
        what the spec actually set: max_model_len always (vLLM's default is
        the model's full context, which rarely fits), dtype when chosen.
        """
        args = ["--max-model-len", str(inference.max_model_len)]
        if inference.dtype and inference.dtype != "auto":
            args += ["--dtype", inference.dtype]
        return args + list(inference.extra_args)

    def stream_command(self, inference: Inference) -> list[str]:
        """The serve command for model_cache="stream", replacing the preset's.

        The preset runs `vllm serve /mnt/models`, and with the
        storage-initializer switched off nothing fills /mnt/models. So the
        command names the object-store URI and the streamer instead. The
        port and the TLS files are the preset's own, so KServe's probes and
        Service still line up -- which is why `options.tls` must match the
        cluster (see _check_tls).
        """
        command = [
            "vllm",
            "serve",
            inference.model,
            "--served-model-name",
            self.served_name(inference),
            "--port",
            str(inference.options.service_port),
            "--load-format",
            "runai_streamer",
        ]
        if inference.gpu_count > 1:
            command += ["--tensor-parallel-size", str(inference.gpu_count)]
        if inference.options.tls:
            command += ["--ssl-certfile", TLS_CERT, "--ssl-keyfile", TLS_KEY]
        return command

    def container(self, inference: Inference) -> dict[str, Any]:
        """Our entry for the preset's `main` container."""
        main: dict[str, Any] = {
            "name": MAIN_CONTAINER,
            "resources": self.resources(inference),
            "args": self.container_args(inference),
        }
        if inference.image or inference.device == "cpu":
            # On CPU the preset's image is no use: it is the CUDA build, and
            # crash-loops on "Failed to infer device type". So a CPU
            # deployment gets the CPU build Inference.container_image
            # resolves, the one the vllm driver runs too.
            main["image"] = inference.container_image
        if inference.options.security_context:
            main["securityContext"] = dict(inference.options.security_context)
        elif inference.device == "cpu" and not inference.image:
            # The CPU build runs as root, and the preset's runAsNonRoot
            # refuses it at start ("image will run as root"). The driver
            # chose this image, so it owns saying how it runs; the rest of
            # the preset's securityContext (no privilege escalation, all
            # capabilities dropped, RuntimeDefault seccomp) still merges in.
            main["securityContext"] = {"runAsNonRoot": False, "runAsUser": 0}

        env = [{"name": k, "value": v} for k, v in sorted(inference.env().items())]
        if inference.model_cache == "stream":
            main["command"] = self.stream_command(inference)
            env += [{"name": k, "value": v} for k, v in sorted(inference.stream_env().items())]
            # By reference, from the same Secret the storage-initializer
            # would have used -- never as values in the manifest.
            env += [
                {
                    "name": key,
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": inference.s3_secret_name,
                            "key": key,
                        }
                    },
                }
                for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
            ]
        if env:
            main["env"] = env
        return main

    def router(self, inference: Inference) -> dict[str, Any] | None:
        options = inference.options
        if options.router == "none":
            return None
        gateway: dict[str, Any] = {}
        if options.gateway_name:
            ref: dict[str, Any] = {"name": options.gateway_name}
            if options.gateway_namespace:
                ref["namespace"] = options.gateway_namespace
            gateway["refs"] = [ref]
        router: dict[str, Any] = {"gateway": gateway, "route": {}}
        if options.scheduler:
            router["scheduler"] = {}
        return router

    def manifests(self, inference: Inference) -> list[dict[str, Any]]:
        """Namespace, the storage ServiceAccount when one is needed, the
        model cache when asked for, the LLMInferenceService, and the optional
        NetworkPolicy. Public so a caller can render and diff it offline."""
        options = inference.options
        namespace = inference.resolved_namespace
        streaming = inference.model_cache == "stream"

        model: dict[str, Any] = {"uri": self.storage_uri(inference)}
        if inference.served_model_name:
            model["name"] = inference.served_model_name

        template: dict[str, Any] = {"containers": [self.container(inference)]}
        # Streaming reads the bucket from the server's own env; only the
        # storage-initializer reads it through a ServiceAccount.
        service_account = None if streaming else self.storage_service_account(inference)
        if service_account:
            template["serviceAccountName"] = service_account
        if inference.node_selector:
            template["nodeSelector"] = dict(inference.node_selector)
        if inference.tolerations:
            template["tolerations"] = list(inference.tolerations)
        if inference.runtime_class:
            template["runtimeClassName"] = inference.runtime_class
        if options.pod_security_context:
            template["securityContext"] = dict(options.pod_security_context)

        spec: dict[str, Any] = {
            "model": model,
            "replicas": inference.replicas,
            "labels": {POD_LABEL: inference.name},
            "template": template,
        }
        if streaming:
            spec["storageInitializer"] = {"enabled": False}
        if inference.device == "gpu" and inference.gpu_count > 1 and not streaming:
            # Stream mode passes the same through its own command, which
            # replaces the preset's -- see stream_command.
            spec["parallelism"] = {"tensor": inference.gpu_count}
        router = self.router(inference)
        if router is not None:
            spec["router"] = router
        if options.base_refs:
            spec["baseRefs"] = [{"name": ref} for ref in options.base_refs]

        objects: list[dict[str, Any]] = [
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": namespace},
            }
        ]
        if service_account and not options.service_account_name:
            objects.append(
                {
                    "apiVersion": "v1",
                    "kind": "ServiceAccount",
                    "metadata": {"name": service_account, "namespace": namespace},
                    "secrets": [{"name": inference.s3_secret_name}],
                }
            )
        objects.extend(self.model_cache_manifests(inference))
        objects.append(
            {
                "apiVersion": LLMISVC_API_VERSION,
                "kind": "LLMInferenceService",
                "metadata": {"name": inference.name, "namespace": namespace},
                "spec": spec,
            }
        )
        if inference.allowed_client_labels:
            objects.append(
                network_policy_manifest(
                    f"{inference.name}-allow-clients",
                    namespace,
                    {POD_LABEL: inference.name},
                    allowed_ingress=inference.allowed_client_labels,
                    ports=[options.service_port],
                )
            )
        return objects

    # -- lifecycle ---------------------------------------------------------
    def create(self, inference: Inference, nodes: list | None = None) -> str:
        inference.validate()
        require_cli("kubectl", error_cls=InferencePrerequisiteError)
        require_cluster(inference.kubeconfig_path, capability="inference")

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
        inference.validate()
        namespace = inference.resolved_namespace
        # KServe garbage-collects the Deployment, Service, route and
        # scheduler it made from this, so only what this driver made is
        # removed by hand.
        self._kubectl(
            inference,
            "delete",
            self.resource_kind,
            inference.name,
            "-n",
            namespace,
            "--ignore-not-found",
        )
        if (
            inference.model_is_s3
            and inference.model_cache != "stream"
            and not inference.options.service_account_name
        ):
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
        if remove_cache:
            self._delete_model_cache(inference)
        print(f"[{self.log_prefix}] removed {inference.name} from {namespace}")

    def endpoint(self, inference: Inference) -> str:
        """`status.url`, which KServe sets to the most reachable address it
        has -- the route's when there is one, else the workload Service's.
        The spec's static form (the workload Service) until it is set.
        vLLM serves /v1 at the root here, so no prefix is appended."""
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
        url = self._status(raw).get("url") or ""
        return url.rstrip("/") if url else inference.endpoint
