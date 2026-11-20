"""
Installing KServe, and what it needs, before any InferenceService exists.

Split out of `kserve.py` because it answers a different question. That
module is about one model deployment; this one is about the platform
underneath it, which is cluster-scoped, installed once, and shared by
every deployment afterwards.

The order is not arbitrary. cert-manager first, because KServe's
admission webhooks are served over TLS it issues. Knative next when the
deployment mode needs it, because KServe's controller decides at startup
what it can reconcile. KServe's CRDs before KServe's controller, since
the controller's own chart ships resources defined by them.

The one piece of real awkwardness is documented at
`_install_controller`: the KServe chart ships ClusterServingRuntimes that
are validated by a webhook the KServe controller itself serves, so a
first install cannot apply them. Confirmed on a live cluster, and the
reason this installs in two passes rather than one.
"""

from __future__ import annotations

import json
import time
from typing import Any

from ...helm import HelmManagerError, HelmRunner
from ...kube import apply as kube_apply
from ...kube import kubectl as kube_kubectl
from ...kube import wait_for_job
from ..base import InferenceError, InferencePrerequisiteError
from ..spec import (
    DEFAULT_MIRROR_IMAGE,
    DEPLOYMENT_MODE_ALIASES,
    KNATIVE_DEPLOYMENT_MODE,
    KSERVE_LOCALMODEL_CHART,
    LOCALMODEL_AGENT_HOST_PATH,
)

# Presence of each of these is how "already installed" is decided. A CRD
# rather than a Helm release: the platform may have been installed by
# anything, and what matters is whether the API exists, not who made it.
ISVC_CRD = "inferenceservices.serving.kserve.io"
LLMISVC_CRD = "llminferenceservices.serving.kserve.io"
LOCALMODEL_CACHE_CRD = "localmodelcaches.serving.kserve.io"
GATEWAY_API_CRD = "httproutes.gateway.networking.k8s.io"
CERT_MANAGER_CRD = "certificates.cert-manager.io"
KNATIVE_OPERATOR_CRD = "knativeservings.operator.knative.dev"
KNATIVE_SERVING_CRD = "services.serving.knative.dev"
# Not installed by this driver in any mode. Its absence is the whole
# reason `disableIstioVirtualHost` has to be turned on -- see
# `_disable_istio_virtual_host`.
ISTIO_VIRTUALSERVICE_CRD = "virtualservices.networking.istio.io"

CERT_MANAGER_RELEASE = "cert-manager"
CERT_MANAGER_CHART = "cert-manager"
KNATIVE_OPERATOR_RELEASE = "knative-operator"
KNATIVE_OPERATOR_CHART = "knative-operator"
KSERVE_CRD_RELEASE = "kserve-crd"
KSERVE_RELEASE = "kserve"
KSERVE_RUNTIME_CONFIGS_RELEASE = "kserve-runtime-configs"
KSERVE_LLMISVC_CRD_RELEASE = "kserve-llmisvc-crd"
KSERVE_LLMISVC_RELEASE = "kserve-llmisvc"
KSERVE_LOCALMODEL_CRD_RELEASE = "kserve-localmodel-crd"
KSERVE_LOCALMODEL_RELEASE = "kserve-localmodel"
# The cache controller's Deployment, whose presence means the chart above
# is already installed.
LOCALMODEL_CONTROLLER = "kserve-localmodel-controller-manager"
# The LLMInferenceService controller, which also reads localModel.
LLMISVC_CONTROLLER = "llmisvc-controller-manager"
KSERVE_LLM_CONFIGS_RELEASE = "kserve-llm-configs"

# The LLMInferenceServiceConfig every LLMInferenceService is merged over.
# Its absence means the presets were never installed, and an
# LLMInferenceService would have no container to run.
LLM_TEMPLATE_CONFIG = "kserve-config-llm-template"

GATEWAY_API_URL = (
    "https://github.com/kubernetes-sigs/gateway-api/releases/download/"
    "{version}/standard-install.yaml"
)

# The label KServe's node agent DaemonSet selects on (the
# `localmodelnode.controller.nodeSelector` default in its charts).
LOCALMODEL_AGENT_LABEL = "kserve/localmodel"
LOCALMODEL_AGENT_VALUE = "worker"
# Which node group a machine belongs to, so the group's local volume can
# be pinned to exactly its machines rather than to every agent node.
MODEL_CACHE_GROUP_LABEL = "multistack.io/model-cache-group"
# The user KServe's agent and download Jobs run as; the cache folder on
# each machine has to be theirs or every download fails on permissions.
LOCALMODEL_UID = 1000

KNATIVE_SERVING_NAME = "knative-serving"

# KServe reads its own ingress settings from here, and the chart's defaults
# name Istio's gateway. Installing Knative with Kourier therefore leaves
# KServe pointed at a Service that does not exist.
ISVC_CONFIGMAP = "inferenceservice-config"
# Kourier's cluster-local Service, which the net-kourier controller creates
# in the Serving namespace alongside the external one.
KOURIER_INTERNAL_SERVICE = "kourier-internal"


class KServePlatformInstaller:
    """Brings a cluster to the point where an InferenceService can exist.

    Idempotent by inspection rather than by retry: each step checks for
    the API it would install and returns early if it is already there, so
    a re-run costs a few `kubectl get crd` calls and a cluster whose
    platform team owns KServe is never upgraded underneath them.
    """

    log_prefix = "inference/kserve"

    def __init__(
        self,
        kubeconfig_path: str,
        command_timeout: int = 300,
        helm: HelmRunner | None = None,
    ) -> None:
        self.kubeconfig_path = kubeconfig_path
        self.command_timeout = command_timeout
        # Injectable so a test can assert on the chart calls without
        # pyhelm3 installed -- it is an optional extra, and this module
        # must import without it.
        self._helm = helm

    # -- entry point --------------------------------------------------------
    def ensure(
        self,
        platform,
        deployment_mode: str,
        llm: bool = False,
        model_cache: bool = False,
    ) -> list[str]:
        """Installs whatever is missing. Returns notes on what it did.

        `llm` adds what an LLMInferenceService needs (its controller,
        presets and the Gateway API); `model_cache` adds KServe's local
        model cache. Both are additions alongside KServe rather than
        upgrades of it, so they apply to a cluster whose KServe someone
        else installed as well as to one this installer just built.

        Raises `InferencePrerequisiteError` when the platform is absent
        and `platform.install` is False, so the message names what to
        install rather than letting a later apply fail on a missing CRD.
        """
        notes: list[str] = []
        have_kserve = self._crd_exists(ISVC_CRD)
        restart = False
        # Normalised once here rather than compared as a string below:
        # KServe 0.20 renamed both modes, and `deployment_mode` arrives as
        # whichever spelling the caller used.
        deployment_mode = DEPLOYMENT_MODE_ALIASES.get(deployment_mode, deployment_mode)
        uses_knative = deployment_mode == KNATIVE_DEPLOYMENT_MODE

        if not have_kserve and not platform.install:
            raise InferencePrerequisiteError(
                f"this cluster has no {ISVC_CRD} CRD and "
                "platform.install is False, so this driver will not install "
                "KServe. Install it by hand, or set "
                "KServePlatform(install=True)."
            )

        # Knative is checked before the "already installed" shortcut
        # below, and deliberately. KServe being present says nothing
        # about whether Serving is: a cluster installed for
        # RawDeployment has no reason to carry it. Returning early on
        # the KServe CRD skipped this, so asking for Serverless on such
        # a cluster installed nothing, reported that it would, and left
        # an InferenceService that could never go ready -- which is the
        # exact failure the Serverless check exists to prevent.
        if uses_knative:
            if platform.install:
                installed_knative = not self._crd_exists(KNATIVE_SERVING_CRD)
                notes.extend(self._install_knative(platform))
                # The controller decides at startup what it can reconcile,
                # so one that came up before Serving existed does not know
                # about it yet.
                restart = installed_knative and have_kserve
            elif not self._crd_exists(KNATIVE_SERVING_CRD):
                raise InferencePrerequisiteError(
                    "deployment_mode='Serverless' needs Knative Serving, "
                    f"but this cluster has no {KNATIVE_SERVING_CRD} CRD and "
                    "platform.install is False. Install Knative by hand, or "
                    "use deployment_mode='RawDeployment'."
                )

        if have_kserve:
            # Do not upgrade a release this driver may not have created.
            # Version pinning applies to what we install, never to what
            # we find.
            notes.append("KServe is already installed; leaving it as it is")
            # Still align the ingress settings. This is the common path --
            # a cluster that had KServe for RawDeployment and is now being
            # asked for Serverless -- and leaving them alone is what makes
            # the InferenceService wait forever on an Istio gateway that
            # was never installed.
            if uses_knative and platform.install:
                aligned, changed = self._align_ingress_config(platform)
                notes.extend(aligned)
                restart = restart or changed
            # And the runtimes are checked here for the same reason Knative
            # is, one branch up: the ISVC CRD existing says nothing about
            # whether this cluster can serve a model. KServe 0.20 moved the
            # built-in runtimes out of `kserve-resources` into their own
            # chart, so "the CRD is there, no runtime is" went from an odd
            # state to the ordinary result of installing the controller
            # alone -- and every InferenceService that relies on
            # `model_format` matching then fails to schedule with
            # `no runtime found to support predictor`, naming a model type
            # rather than the missing chart.
            #
            # Additive and only when there are none: unlike the controller
            # release, ClusterServingRuntimes are cluster-scoped resources
            # that nothing else owns, and a cluster that already has some
            # keeps them untouched. `install_runtime_configs=False` remains
            # the opt-out for a cluster whose runtimes someone else manages.
            if platform.install and not self._serving_runtimes_exist():
                notes.extend(self._install_runtime_configs(platform))
            if platform.install:
                disabled, turned_off = self._disable_istio_virtual_host(platform)
                notes.extend(disabled)
                restart = restart or turned_off
            if restart:
                notes.extend(self._restart_controller(platform))
            notes.extend(self._ensure_additions(platform, llm, model_cache))
            return notes

        if platform.cert_manager:
            notes.extend(self._install_cert_manager(platform))

        notes.extend(self._install_crds(platform))
        notes.extend(
            self._install_controller(
                platform,
                deployment_mode,
                model_cache=model_cache,
            )
        )
        # The chart just wrote the ConfigMap with Istio's defaults, and the
        # controller has already read them, so a change here needs the
        # restart even on a fresh install.
        changed = False
        if uses_knative:
            aligned, gateway_changed = self._align_ingress_config(platform)
            notes.extend(aligned)
            changed = changed or gateway_changed
        # Every mode, not just Knative: nothing here installs Istio.
        disabled, turned_off = self._disable_istio_virtual_host(platform)
        notes.extend(disabled)
        changed = changed or turned_off
        if changed:
            notes.extend(self._restart_controller(platform))
        notes.extend(self._ensure_additions(platform, llm, model_cache))
        return notes

    # -- additions alongside KServe -----------------------------------------
    def _ensure_additions(self, platform, llm: bool, model_cache: bool) -> list[str]:
        """LLMInferenceService and the model cache, each only when asked.

        Neither upgrades the KServe release, which is why both run on a
        cluster whose KServe was found rather than installed. What cannot
        be added without that upgrade -- turning on the cache controller
        inside someone else's KServe -- is reported, not forced.
        """
        if not platform.install:
            return []
        notes: list[str] = []
        if llm:
            notes.extend(self._ensure_gateway_api(platform))
            notes.extend(self._ensure_llmisvc(platform))
        if model_cache:
            notes.extend(self._ensure_model_cache(platform))
        return notes

    def _ensure_gateway_api(self, platform) -> list[str]:
        """The Gateway API CRDs, which the llmisvc controller watches.

        Applied from the upstream release rather than a chart: the Gateway
        API publishes its CRDs as one manifest, and it is the same one
        `multistack/route` tells users to apply by hand. Server-side, since
        the CRDs are large enough to overflow the client-side
        last-applied annotation.
        """
        if not platform.gateway_api_version:
            return []
        if self._crd_exists(GATEWAY_API_CRD):
            return ["Gateway API CRDs already present"]
        url = GATEWAY_API_URL.format(version=platform.gateway_api_version)
        self._log(f"installing the Gateway API CRDs {platform.gateway_api_version}")
        self._kubectl("apply", "--server-side", "-f", url)
        return [f"installed the Gateway API CRDs {platform.gateway_api_version}"]

    def _ensure_llmisvc(self, platform) -> list[str]:
        """LLMInferenceService's CRDs and controller, then its presets.

        Two charts of KServe's own. `createSharedResources` is off because
        `kserve-resources` already created the shared ConfigMap and
        webhook objects -- a second chart rendering them fails Helm's
        ownership check on objects another release owns.
        """
        notes: list[str] = []
        if self._crd_exists(LLMISVC_CRD):
            notes.append("LLMInferenceService already installed")
        else:
            self._log("installing the LLMInferenceService CRDs")
            self._install(
                KSERVE_LLMISVC_CRD_RELEASE,
                chart=platform.llmisvc_crd_chart,
                namespace=platform.namespace,
                chart_version=platform.chart_version,
                timeout=platform.install_timeout,
            )
            self._log("installing the LLMInferenceService controller")
            self._install(
                KSERVE_LLMISVC_RELEASE,
                chart=platform.llmisvc_chart,
                namespace=platform.namespace,
                chart_version=platform.chart_version,
                values={"kserve": {"createSharedResources": False}},
                timeout=platform.install_timeout,
                atomic=False,
            )
            notes.append(
                f"installed LLMInferenceService {platform.chart_version or ''}".rstrip()
                + f" in {platform.namespace}"
            )
        notes.extend(self._ensure_llm_presets(platform))
        return notes

    def _ensure_llm_presets(self, platform) -> list[str]:
        """KServe's LLMInferenceServiceConfig presets.

        Every LLMInferenceService is merged over `kserve-config-llm-template`
        -- that is where vLLM's command, probes and volumes come from -- so
        without the presets there is nothing to run.

        They ship in `kserve-runtime-configs`, the same chart as the
        ClusterServingRuntimes, but go in as a release of their own with
        only the presets switched on. Upgrading the runtimes' release to add
        them would re-render the runtimes too, and on a cluster where that
        release has them off, would remove them.
        """
        if self._exists(
            "llminferenceserviceconfig", LLM_TEMPLATE_CONFIG, namespace=platform.namespace
        ):
            return []
        self._log("installing the LLMInferenceService presets")
        self._install(
            KSERVE_LLM_CONFIGS_RELEASE,
            chart=platform.runtime_configs_chart,
            namespace=platform.namespace,
            chart_version=platform.chart_version,
            values={
                "kserve": {
                    "servingruntime": {"enabled": False},
                    "llmisvcConfigs": {"enabled": True},
                }
            },
            timeout=platform.install_timeout,
            atomic=False,
        )
        return [f"installed the LLMInferenceService presets in {platform.namespace}"]

    def _ensure_model_cache(self, platform) -> list[str]:
        """KServe's local model cache, on the machines the platform names.

        In order: the CRDs and the cache's controller and node agent, if
        missing; the labels the agent and the node group's volume select on;
        the cache folder on each machine, owned by the user KServe's agent
        runs as; then the StorageClass and the LocalModelNodeGroup itself.
        """
        cache = platform.model_cache
        if not cache.enabled:
            raise InferencePrerequisiteError(
                "a deployment asked for model_cache='node' but "
                "KServePlatform.model_cache.enabled is False, so no machine "
                "is set up to hold a copy. Enable it and list the GPU nodes "
                "in model_cache.nodes."
            )
        if cache.path != LOCALMODEL_AGENT_HOST_PATH:
            # Refused rather than warned: the node group's volume would point
            # at one folder while KServe's agent watches another, and the
            # agent then re-runs the download forever without an error.
            raise InferencePrerequisiteError(
                f"model_cache.path={cache.path!r}, but KServe {platform.chart_version}'s "
                f"node agent only ever uses {LOCALMODEL_AGENT_HOST_PATH!r} -- its "
                "chart hardcodes that path. Leave model_cache.path at its "
                "default, or mount the dedicated disk there."
            )
        notes: list[str] = []
        if not self._crd_exists(LOCALMODEL_CACHE_CRD):
            self._log("installing the LocalModelCache CRDs")
            self._install(
                KSERVE_LOCALMODEL_CRD_RELEASE,
                chart=platform.localmodel_crd_chart,
                namespace=platform.namespace,
                chart_version=platform.chart_version,
                timeout=platform.install_timeout,
            )
            notes.append("installed the LocalModelCache CRDs")

        if not self._exists("deployment", LOCALMODEL_CONTROLLER, namespace=platform.namespace):
            self._log("installing the local model cache controller and node agent")
            self._install(
                KSERVE_LOCALMODEL_RELEASE,
                chart=KSERVE_LOCALMODEL_CHART,
                namespace=platform.namespace,
                chart_version=platform.chart_version,
                values={"kserve": {"createSharedResources": False}},
                timeout=platform.install_timeout,
                atomic=False,
            )
            notes.append("installed the local model cache controller and node agent")

        notes.extend(self._check_cache_controller(platform))

        for node in cache.nodes:
            self._kubectl(
                "label",
                "node",
                node,
                f"{LOCALMODEL_AGENT_LABEL}={LOCALMODEL_AGENT_VALUE}",
                f"{MODEL_CACHE_GROUP_LABEL}={cache.node_group}",
                "--overwrite",
            )
        notes.append(
            f"labelled {len(cache.nodes)} node(s) for model cache group {cache.node_group}"
        )

        # A Job's pod template is immutable, so an earlier prep Job (a re-run,
        # or one from before its command changed) is replaced, not re-applied.
        for node in cache.nodes:
            self._kubectl(
                "delete",
                "job",
                self._prep_job_name(node),
                "-n",
                cache.job_namespace,
                "--ignore-not-found",
                "--wait=true",
            )
        kube_apply(
            self.kubeconfig_path,
            self.model_cache_prep_manifests(platform),
            timeout=self.command_timeout,
            error_cls=InferenceError,
        )
        for node in cache.nodes:
            wait_for_job(
                self.kubeconfig_path,
                self._prep_job_name(node),
                cache.job_namespace,
                timeout=platform.install_timeout,
                error_cls=InferenceError,
            )
        notes.append(f"prepared {cache.path} on each cache node")

        kube_apply(
            self.kubeconfig_path,
            self.model_cache_manifests(platform),
            timeout=self.command_timeout,
            error_cls=InferenceError,
        )
        notes.append(
            f"LocalModelNodeGroup {cache.node_group}: {cache.size_gb}Gi at "
            f"{cache.path} on {', '.join(cache.nodes)}"
        )
        return notes

    def _check_cache_controller(self, platform) -> list[str]:
        """Turns KServe's own local model cache switch on if it is off.

        The switch is `localModel.enabled` in `inferenceservice-config`.
        With it off, everything else still looks right: the cache is
        accepted, KServe's agent downloads it, the model reaches Ready --
        and the model pod downloads the weights again on every start,
        because nothing connects it to the cache. Seen on a live KServe
        0.20 cluster whose KServe had been installed without the cache.

        Set by patching that one key, as `_disable_istio_virtual_host`
        patches `ingress`, rather than by upgrading the KServe release: this
        only runs with `platform.install` set, where the platform is this
        installer's to configure, and a found KServe keeps every other
        value. The two controllers that read it are restarted so they see
        it. If the setting cannot be read or written, the deployment is
        refused rather than left to serve without its cache.
        """
        raw = self._kubectl(
            "get",
            "configmap",
            ISVC_CONFIGMAP,
            "-n",
            platform.namespace,
            "-o",
            "jsonpath={.data.localModel}",
            check=False,
        )
        try:
            local_model = json.loads(raw) if raw.strip() else None
        except ValueError:
            local_model = None
        if not isinstance(local_model, dict):
            raise InferencePrerequisiteError(
                f"could not read localModel from {ISVC_CONFIGMAP} in "
                f"{platform.namespace}, so KServe's local model cache cannot be "
                "switched on. Without it a model_cache='node' deployment "
                "downloads its weights on every start."
            )
        if local_model.get("enabled") is True:
            return []

        local_model["enabled"] = True
        local_model.setdefault("jobNamespace", platform.model_cache.job_namespace)
        self._log(f"switching on KServe's local model cache in {ISVC_CONFIGMAP}")
        self._kubectl(
            "patch",
            "configmap",
            ISVC_CONFIGMAP,
            "-n",
            platform.namespace,
            "--type",
            "merge",
            "-p",
            json.dumps({"data": {"localModel": json.dumps(local_model, indent=2)}}),
        )
        for controller in ("kserve-controller-manager", LLMISVC_CONTROLLER):
            if self._exists("deployment", controller, namespace=platform.namespace):
                self._kubectl(
                    "rollout",
                    "restart",
                    f"deployment/{controller}",
                    "-n",
                    platform.namespace,
                )
                self._kubectl(
                    "rollout",
                    "status",
                    f"deployment/{controller}",
                    "-n",
                    platform.namespace,
                    f"--timeout={platform.install_timeout}s",
                )
        return [
            f"switched on KServe's local model cache in {ISVC_CONFIGMAP} "
            "and restarted its controllers"
        ]

    @staticmethod
    def _prep_job_name(node: str) -> str:
        # Node names are DNS labels already; trimmed to leave room for the
        # prefix inside the 63-character limit.
        return f"model-cache-prep-{node}"[:63].rstrip("-")

    def model_cache_prep_manifests(self, platform) -> list[dict[str, Any]]:
        """One Job per cache node, creating the cache folder and handing it
        to the user KServe's agent runs as.

        KServe documents this as a manual step, and its absence is quiet:
        the agent cannot write, and its download Jobs fail one after
        another. A Job per node, pinned with `nodeName`, does it without SSH.
        The pinned mirror image is reused because it already has a shell
        and coreutils and is already trusted by digest.

        Owning the top folder is not enough, and the setgid bit is the fix.
        Each model lives at `models/<key>` below it, and whoever mounts that
        path first creates it: when a model pod is scheduled before the agent
        has made the folder, the kubelet makes it -- as root. KServe's
        download Job then runs as UID 1000 and fails with "Permission
        denied", forever, while the model pod waits on an empty folder.
        Found on a live cluster. With setgid on `models/`, a folder the
        kubelet creates there inherits group 1000, which the download Job
        has through its `fsGroup`, so it stays writable whoever creates it.
        `chown -R` repairs a tree an earlier run left root-owned.
        """
        cache = platform.model_cache
        objects: list[dict[str, Any]] = [
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": cache.job_namespace},
            }
        ]
        for node in cache.nodes:
            objects.append(
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": {
                        "name": self._prep_job_name(node),
                        "namespace": cache.job_namespace,
                    },
                    "spec": {
                        "backoffLimit": 1,
                        "ttlSecondsAfterFinished": 600,
                        "template": {
                            "spec": {
                                "nodeName": node,
                                "restartPolicy": "Never",
                                "containers": [
                                    {
                                        "name": "prepare",
                                        "image": DEFAULT_MIRROR_IMAGE,
                                        "command": [
                                            "sh",
                                            "-c",
                                            "mkdir -p /cache/models"
                                            f" && chown -R {LOCALMODEL_UID}:{LOCALMODEL_UID} /cache"
                                            " && chmod 2775 /cache /cache/models",
                                        ],
                                        "securityContext": {"runAsUser": 0},
                                        "volumeMounts": [{"name": "cache", "mountPath": "/cache"}],
                                    }
                                ],
                                "volumes": [
                                    {
                                        "name": "cache",
                                        "hostPath": {
                                            "path": cache.path,
                                            "type": "DirectoryOrCreate",
                                        },
                                    }
                                ],
                            }
                        },
                    },
                }
            )
        return objects

    def model_cache_manifests(self, platform) -> list[dict[str, Any]]:
        """The StorageClass and LocalModelNodeGroup for the cache.

        The group's volume is a local PersistentVolume at `path`, pinned to
        the machines carrying this group's label -- which is what keeps a
        pod reading a cached model on a machine that actually has it. The
        StorageClass has no provisioner: KServe creates the volumes from
        the group's template, and WaitForFirstConsumer defers binding until
        a pod is placed, so the volume follows the pod's node.
        """
        cache = platform.model_cache
        size = f"{cache.size_gb}Gi"
        return [
            {
                "apiVersion": "storage.k8s.io/v1",
                "kind": "StorageClass",
                "metadata": {"name": cache.storage_class},
                "provisioner": "kubernetes.io/no-provisioner",
                "volumeBindingMode": "WaitForFirstConsumer",
            },
            {
                "apiVersion": "serving.kserve.io/v1alpha1",
                "kind": "LocalModelNodeGroup",
                "metadata": {"name": cache.node_group},
                "spec": {
                    "storageLimit": size,
                    "persistentVolumeClaimSpec": {
                        "accessModes": ["ReadWriteOnce"],
                        "resources": {"requests": {"storage": size}},
                        "storageClassName": cache.storage_class,
                        "volumeMode": "Filesystem",
                    },
                    "persistentVolumeSpec": {
                        "accessModes": ["ReadWriteOnce"],
                        "volumeMode": "Filesystem",
                        "capacity": {"storage": size},
                        "local": {"path": cache.path},
                        "storageClassName": cache.storage_class,
                        "nodeAffinity": {
                            "required": {
                                "nodeSelectorTerms": [
                                    {
                                        "matchExpressions": [
                                            {
                                                "key": MODEL_CACHE_GROUP_LABEL,
                                                "operator": "In",
                                                "values": [cache.node_group],
                                            }
                                        ],
                                    }
                                ]
                            }
                        },
                    },
                },
            },
        ]

    def _disable_istio_virtual_host(self, platform) -> tuple[list[str], bool]:
        """Stops KServe waiting on a VirtualService it cannot create.

        `disableIstioVirtualHost` defaults to false, so the controller
        tries to reconcile an Istio VirtualService for every
        InferenceService. On a cluster with no Istio the CRD is not there,
        so it logs

            Istio VirtualService CRD not present; VirtualService
            reconciliation skipped

        and -- having skipped it -- leaves the InferenceService at

            IngressReady  False  "Predictor ingress not created"
            Ready         False  "Predictor ingress not created"

        forever. The message reads as advice and is a blocker: a predictor
        whose model loaded perfectly never goes ready.

        `_align_ingress_config` below sets this too, but only for Knative
        with Kourier, which left Standard mode -- the default, and the mode
        with no Knative at all -- pointed at Istio with nothing to point
        at. Observed live on 2026-10-09 under deploymentMode=Standard.

        Keyed on the CRD rather than on the mode or on a spec field: what
        matters is whether this cluster can serve a VirtualService, and an
        Istio cluster keeps the default so a platform team's own gateway
        settings are left alone.
        """
        if self._crd_exists(ISTIO_VIRTUALSERVICE_CRD):
            return [], False

        raw = self._kubectl(
            "get",
            "configmap",
            ISVC_CONFIGMAP,
            "-n",
            platform.namespace,
            "-o",
            "jsonpath={.data.ingress}",
            check=False,
        )
        if not raw.strip():
            return [
                f"could not read {ISVC_CONFIGMAP}'s ingress settings in "
                f"{platform.namespace}, so disableIstioVirtualHost may still "
                "be false; an InferenceService would then stay "
                "'Predictor ingress not created' however healthy it is"
            ], False
        try:
            ingress = json.loads(raw)
        except ValueError:
            return [
                f"{ISVC_CONFIGMAP}'s ingress settings are not valid JSON, so they were left alone"
            ], False

        if ingress.get("disableIstioVirtualHost") is True:
            return [], False

        ingress["disableIstioVirtualHost"] = True
        self._log(
            "this cluster has no Istio VirtualService CRD, so turning off "
            "KServe's VirtualService reconciliation"
        )
        self._kubectl(
            "patch",
            "configmap",
            ISVC_CONFIGMAP,
            "-n",
            platform.namespace,
            "--type",
            "merge",
            "-p",
            json.dumps({"data": {"ingress": json.dumps(ingress, indent=4)}}),
        )
        # Caller restarts, for the same reason as below: one rolling
        # restart per call, not one per setting.
        return [
            "set disableIstioVirtualHost=true -- this cluster has no "
            f"{ISTIO_VIRTUALSERVICE_CRD}, and leaving it false holds every "
            "InferenceService at 'Predictor ingress not created'"
        ], True

    def _align_ingress_config(self, platform) -> tuple[list[str], bool]:
        """Point KServe's ingress settings at the gateway this cluster has.

        KServe's chart defaults name Istio:

            localGatewayService: knative-local-gateway.istio-system.svc...
            ingressClassName:    istio
            disableIstioVirtualHost: false

        Those are wrong for every cluster whose Knative uses Kourier --
        which is this driver's own default, so the default combination was
        broken. KServe resolves a Service in a namespace that does not
        exist, and the InferenceService reports

            Ready  Unknown  "Waiting for load balancer to be ready"

        which names neither Istio nor the real problem, and is why this was
        first misread as needing a LoadBalancer. Observed live: the Knative
        Route was Ready with a working cluster-local URL while the
        InferenceService above it waited on a gateway in a missing
        namespace.

        Only touches the keys that are implementation-specific, and only
        for Kourier -- an Istio cluster already matches the defaults, and
        rewriting them there would undo a platform team's own settings.
        """
        if platform.knative_ingress != "kourier":
            return [], False

        raw = self._kubectl(
            "get",
            "configmap",
            ISVC_CONFIGMAP,
            "-n",
            platform.namespace,
            "-o",
            "jsonpath={.data.ingress}",
            check=False,
        )
        if not raw.strip():
            # No ConfigMap to align, which a freshly installed KServe
            # always has -- so this means something else is wrong, and
            # saying so beats patching a resource that is not there.
            return [
                f"could not read {ISVC_CONFIGMAP}'s ingress settings in "
                f"{platform.namespace}, so KServe may still be pointed at "
                "Istio's gateway; a Serverless InferenceService would then "
                "wait on a load balancer that is not the real problem"
            ], False
        try:
            ingress = json.loads(raw)
        except ValueError:
            return [
                f"{ISVC_CONFIGMAP}'s ingress settings are not valid JSON, so they were left alone"
            ], False

        gateway_service = (
            f"{KOURIER_INTERNAL_SERVICE}.{platform.knative_serving_namespace}.svc.cluster.local"
        )
        wanted = {
            "localGatewayService": gateway_service,
            "knativeLocalGatewayService": gateway_service,
            "ingressClassName": platform.knative_ingress,
            # With Kourier there is no Istio VirtualService to create, and
            # leaving this false is what makes KServe block.
            "disableIstioVirtualHost": True,
        }
        if all(ingress.get(k) == v for k, v in wanted.items()):
            return [], False

        ingress.update(wanted)
        self._log(f"pointing KServe's ingress config at {gateway_service}")
        self._kubectl(
            "patch",
            "configmap",
            ISVC_CONFIGMAP,
            "-n",
            platform.namespace,
            "--type",
            "merge",
            "-p",
            json.dumps({"data": {"ingress": json.dumps(ingress, indent=4)}}),
        )
        # The controller reads this at startup, so a patch alone changes
        # nothing until it restarts.
        # The caller restarts, not this method: `ensure` may already owe a
        # restart because Serving landed after the controller, and two
        # rolling restarts in one call is one more than the cluster needs.
        return [
            f"pointed KServe's ingress config at {gateway_service} "
            f"({platform.knative_ingress}), replacing the chart's Istio "
            "defaults"
        ], True

    def _restart_controller(self, platform) -> list[str]:
        """Rolls the KServe controller so it re-reads what the cluster can
        do.

        Two callers now, both because the controller reads at startup:
        Serving landing alongside a KServe that was already running, and
        a change to the ingress settings in `inferenceservice-config`.
        The message said "to pick up Knative" for both, which was wrong
        half the time and sent the reader looking for a Knative problem
        on a cluster with no Knative at all.
        """
        self._log("restarting the KServe controller so it re-reads its config")
        self._kubectl(
            "rollout",
            "restart",
            "deployment/kserve-controller-manager",
            "-n",
            platform.namespace,
            check=False,
        )
        self._wait_for_webhook(platform)
        return ["restarted the KServe controller so it re-reads its config"]

    # -- cert-manager -------------------------------------------------------
    def _install_cert_manager(self, platform) -> list[str]:
        """KServe's webhooks are served over TLS cert-manager issues, so
        this precedes everything else."""
        if self._crd_exists(CERT_MANAGER_CRD):
            return ["cert-manager already present"]

        self._log("installing cert-manager")
        self._install(
            CERT_MANAGER_RELEASE,
            chart=CERT_MANAGER_CHART,
            namespace=platform.cert_manager_namespace,
            chart_version=platform.cert_manager_chart_version,
            # The chart does not install its own CRDs by default, and
            # KServe's Certificate resources need them.
            values={"crds": {"enabled": True}},
            timeout=platform.install_timeout,
        )
        return [f"installed cert-manager in {platform.cert_manager_namespace}"]

    # -- knative ------------------------------------------------------------
    def _install_knative(self, platform) -> list[str]:
        """Serverless mode only.

        Knative publishes the *operator* as a chart; Serving itself is a
        `KnativeServing` resource the operator reconciles. The ingress
        layer is a field on that resource, which is why nothing here
        installs Kourier or Istio as a chart of its own.
        """
        notes: list[str] = []

        if not self._crd_exists(KNATIVE_OPERATOR_CRD):
            self._log("installing the Knative operator")
            self._install(
                KNATIVE_OPERATOR_RELEASE,
                chart=KNATIVE_OPERATOR_CHART,
                namespace=platform.knative_operator_namespace,
                chart_version=platform.knative_chart_version,
                timeout=platform.install_timeout,
            )
            notes.append(f"installed the Knative operator in {platform.knative_operator_namespace}")

        if self._crd_exists(KNATIVE_SERVING_CRD):
            notes.append("Knative Serving already present")
            return notes

        self._log("creating the KnativeServing resource")
        kube_apply(
            self.kubeconfig_path,
            self.knative_serving_manifests(platform),
            timeout=self.command_timeout,
            error_cls=InferenceError,
        )
        # The resource's own Ready condition, not the CRD it produces.
        # The operator creates `services.serving.knative.dev` early and
        # then spends another half-minute bringing up the activator,
        # autoscaler, webhook and ingress -- so waiting on the CRD
        # returns while Serving cannot yet serve, and a Serverless
        # InferenceService created in that window has nothing to
        # reconcile it.
        self._wait_for_knative(platform)
        notes.append(
            f"Knative Serving reconciled in {platform.knative_serving_namespace} "
            f"with {platform.knative_ingress} ingress"
        )
        return notes

    def knative_serving_manifests(self, platform) -> list[dict[str, Any]]:
        """The namespace and the KnativeServing resource. Public so a
        caller can see what would be created without a cluster."""
        namespace = platform.knative_serving_namespace

        ingress: dict[str, Any] = {"enabled": True}
        if platform.knative_ingress_service_type:
            # The operator owns the gateway's Service and reconciles it
            # back, so this is the only place the type can be set and
            # stay set -- see KServePlatform.knative_ingress_service_type.
            ingress["service-type"] = platform.knative_ingress_service_type

        return [
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": namespace},
            },
            {
                "apiVersion": "operator.knative.dev/v1beta1",
                "kind": "KnativeServing",
                "metadata": {"name": KNATIVE_SERVING_NAME, "namespace": namespace},
                "spec": {
                    "ingress": {platform.knative_ingress: ingress},
                    "config": {
                        "network": {
                            "ingress-class": f"{platform.knative_ingress}"
                            ".ingress.networking.knative.dev",
                        },
                    },
                },
            },
        ]

    # -- kserve -------------------------------------------------------------
    def _install_crds(self, platform) -> list[str]:
        self._log("installing the KServe CRDs")
        self._install(
            KSERVE_CRD_RELEASE,
            chart=platform.crd_chart,
            namespace=platform.namespace,
            chart_version=platform.chart_version,
            timeout=platform.install_timeout,
        )
        return [f"installed the KServe CRDs in {platform.namespace}"]

    def controller_values(
        self,
        platform,
        deployment_mode: str,
        model_cache: bool = False,
    ) -> dict[str, Any]:
        """Values for `kserve-resources`. Public so a test can read them.

        `kserve.localmodel.enabled` writes the `localModel` block of
        inferenceservice-config, which is what makes KServe use a matching
        cache at all. It deploys nothing -- the cache's controller and agent
        are a chart of their own (see _ensure_model_cache). So it is set
        here, at install, where the release is ours to configure.
        """
        values: dict[str, Any] = {
            "kserve": {"controller": {"deploymentMode": deployment_mode}},
        }
        cache = platform.model_cache
        if model_cache and cache.enabled:
            values["kserve"]["localmodel"] = {
                "enabled": True,
                "jobNamespace": cache.job_namespace,
            }
        return values

    def _install_controller(
        self,
        platform,
        deployment_mode: str,
        model_cache: bool = False,
    ) -> list[str]:
        """Installs the controller, in two passes, then the runtimes.

        The charts carry ClusterServingRuntimes, and those are validated by
        a webhook the KServe controller itself serves. On a first install
        that pod is not running when a chart applies them, so they fail with
        `connection refused` while the controller comes up perfectly well
        beside them.

        So the first pass runs non-atomically -- rolling back would discard
        the healthy controller along with the runtimes that could not apply
        -- and the second pass, once the webhook answers, applies them. Only
        a failure on the second pass is real.

        The built-in runtimes then come from their own chart, which is where
        KServe 0.20 moved them: `kserve-resources` shipped thirteen at v0.15
        and ships one at v0.20. It goes in after the webhook is answering,
        for the same reason the second pass does.
        """
        values = self.controller_values(platform, deployment_mode, model_cache)

        self._log(f"installing the KServe controller ({deployment_mode})")
        try:
            self._install(
                KSERVE_RELEASE,
                chart=platform.controller_chart,
                namespace=platform.namespace,
                chart_version=platform.chart_version,
                values=values,
                timeout=platform.install_timeout,
                atomic=False,
            )
        except HelmManagerError as first_pass:
            self._log(
                "first pass did not complete, which is expected while the "
                f"admission webhook is still starting: {first_pass}"
            )

        self._wait_for_webhook(platform)

        self._log("applying the serving runtimes")
        self._install(
            KSERVE_RELEASE,
            chart=platform.controller_chart,
            namespace=platform.namespace,
            chart_version=platform.chart_version,
            values=values,
            timeout=platform.install_timeout,
            atomic=False,
        )
        notes = [
            f"installed KServe {platform.chart_version or ''}".rstrip()
            + f" in {platform.namespace}, deploymentMode={deployment_mode}"
        ]
        notes.extend(self._install_runtime_configs(platform))
        return notes

    def _install_runtime_configs(self, platform) -> list[str]:
        """The built-in ClusterServingRuntimes, their own chart since 0.20.

        Skipped when `install_runtime_configs` is False, which is for a
        cluster whose runtimes someone else owns. A spec that names its own
        `runtime` needs nothing from here; one that relies on KServe
        matching `model_format` needs all of it, and without it the
        InferenceService waits on a runtime that was never installed.
        """
        if not platform.install_runtime_configs:
            return [
                "skipped the built-in serving runtimes "
                "(install_runtime_configs=False); a spec that does not name "
                "its own `runtime` will have nothing to match against"
            ]

        self._log("installing the built-in serving runtimes")
        self._install(
            KSERVE_RUNTIME_CONFIGS_RELEASE,
            chart=platform.runtime_configs_chart,
            namespace=platform.namespace,
            chart_version=platform.chart_version,
            # Switched on explicitly. The chart's own default is
            # `servingruntime.enabled: false`, and its template renders the
            # runtimes only under that flag -- installed with no values it
            # succeeds and creates nothing, which is the failure this chart
            # was added to prevent. The LLM presets stay off here; they are
            # a release of their own (see _ensure_llm_presets).
            values={
                "kserve": {
                    "servingruntime": {"enabled": True},
                    "llmisvcConfigs": {"enabled": False},
                }
            },
            timeout=platform.install_timeout,
            atomic=False,
        )
        return [
            f"installed the built-in serving runtimes {platform.chart_version or ''}".rstrip()
            + f" in {platform.namespace}"
        ]

    def _wait_for_webhook(self, platform) -> None:
        """Waits for the controller to be able to answer its own webhook.

        Deployment availability rather than pod phase: a pod is Running
        while its containers are unready, including between crash-loop
        restarts, so polling the phase would report success on a
        controller that never served anything.
        """
        deadline = time.time() + platform.install_timeout
        while time.time() < deadline:
            available = self._kubectl(
                "get",
                "deployment",
                "kserve-controller-manager",
                "-n",
                platform.namespace,
                "-o",
                "jsonpath={.status.availableReplicas}",
                check=False,
            ).strip()
            if available and available != "0":
                return
            time.sleep(5)

        raise InferenceError(
            "the KServe controller did not become available within "
            f"{platform.install_timeout}s, so its admission webhook never "
            "answered and the serving runtimes could not be applied. Check "
            f"`kubectl -n {platform.namespace} logs "
            "deploy/kserve-controller-manager --kubeconfig "
            f"{self.kubeconfig_path}`."
        )

    # -- plumbing -----------------------------------------------------------
    def _install(
        self,
        release: str,
        *,
        chart: str,
        namespace: str,
        chart_version: str | None = None,
        values: dict[str, Any] | None = None,
        timeout: int = 900,
        atomic: bool = True,
    ):
        return self.helm().install_or_upgrade(
            release,
            chart=chart,
            namespace=namespace,
            chart_version=chart_version,
            values=values or {},
            atomic=atomic,
            wait=True,
            create_namespace=True,
        )

    def helm(self) -> HelmRunner:
        """The Helm facade, built on first use.

        Not in `__init__`: constructing one reaches for pyhelm3, which is
        an optional extra, and a caller that never installs anything
        should not need it.
        """
        if self._helm is None:
            self._helm = HelmRunner(self.kubeconfig_path)
        return self._helm

    def _serving_runtimes_exist(self) -> bool:
        """Whether anything in this cluster can serve a model.

        Cluster-scoped only. A namespaced ServingRuntime serves its own
        namespace, so one in `default` says nothing about whether the
        namespace an InferenceService is going into has a match.
        """
        found = self._kubectl("get", "clusterservingruntimes", "-o", "name", check=False)
        return bool(found.strip())

    def _crd_exists(self, name: str) -> bool:
        return self._exists("crd", name)

    def _exists(self, kind: str, name: str, namespace: str | None = None) -> bool:
        args = ["get", kind, name, "-o", "name"]
        if namespace:
            args += ["-n", namespace]
        return bool(self._kubectl(*args, check=False).strip())

    def _wait_for_knative(self, platform) -> None:
        """Waits for the operator to finish reconciling Serving."""
        deadline = time.time() + platform.install_timeout
        while time.time() < deadline:
            ready = self._kubectl(
                "get",
                "knativeserving",
                KNATIVE_SERVING_NAME,
                "-n",
                platform.knative_serving_namespace,
                "-o",
                "jsonpath={.status.conditions[?(@.type=='Ready')].status}",
                check=False,
            ).strip()
            if ready == "True":
                return
            time.sleep(5)

        raise InferenceError(
            "Knative Serving did not become ready within "
            f"{platform.install_timeout}s. Check `kubectl -n "
            f"{platform.knative_serving_namespace} get knativeserving "
            f"{KNATIVE_SERVING_NAME} -o yaml --kubeconfig "
            f"{self.kubeconfig_path}` for what the operator is stuck on."
        )

    def _kubectl(self, *args: str, check: bool = True) -> str:
        return kube_kubectl(
            self.kubeconfig_path,
            *args,
            check=check,
            timeout=self.command_timeout,
            error_cls=InferenceError,
        )

    def _log(self, message: str) -> None:
        print(f"[{self.log_prefix}] {message}")
