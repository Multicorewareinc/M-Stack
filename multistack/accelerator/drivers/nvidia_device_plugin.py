"""NVIDIA's k8s-device-plugin, installed from its chart via the Helm layer.

The plugin's whole job is to advertise `nvidia.com/gpu` on the nodes that
have a card. Everything interesting here is about the two ways that
silently does not happen -- see `check_prerequisites` and the tail of
`create`.
"""
import json
from typing import Any, Dict, List

from ...helm import HelmRunner
from ...kube import kubectl, require_cli, require_cluster
from ..base import AcceleratorError, AcceleratorPrerequisiteError
from ..spec import NODE_FEATURE_LABEL, Accelerator

# What the chart labels its DaemonSet with, which is how the install is
# checked without depending on the release name.
DAEMONSET_SELECTOR = "app.kubernetes.io/name=nvidia-device-plugin"


class NvidiaDevicePluginDriver:
    def __init__(self, ready_timeout: int = 300) -> None:
        self._ready_timeout = ready_timeout

    def _helm(self, accelerator: Accelerator) -> HelmRunner:
        return HelmRunner(
            accelerator.kubeconfig_path, error_cls=AcceleratorError
        )

    def _values(self, accelerator: Accelerator) -> Dict[str, Any]:
        options = accelerator.options
        values: Dict[str, Any] = {
            "runtimeClassName": options.runtime_class_name,
        }
        # Absent leaves the chart's own default in place; an explicit {}
        # overrides it with "every node", which is a different statement
        # and sometimes the intended one.
        if accelerator.node_selector is not None:
            values["nodeSelector"] = accelerator.node_selector
        if accelerator.tolerations is not None:
            values["tolerations"] = accelerator.tolerations
        return values

    def _nodes(self, accelerator: Accelerator) -> List[Dict[str, Any]]:
        """The nodes this install targets, as the API server reports them.

        Empty when the spec names no selector -- there is nothing to
        narrow to, and the chart's own affinity decides.
        """
        if not accelerator.node_selector:
            return []
        selector = ",".join(
            f"{key}={value}"
            for key, value in accelerator.node_selector.items()
        )
        raw = kubectl(
            accelerator.kubeconfig_path, "get", "nodes",
            "-l", selector, "-o", "json",
            error_cls=AcceleratorError, check=False,
        )
        if not (raw or "").strip():
            return []
        try:
            return json.loads(raw).get("items", [])
        except json.JSONDecodeError:
            return []

    def check_prerequisites(self, accelerator: Accelerator) -> List[str]:
        warnings: List[str] = []
        require_cli("helm", error_cls=AcceleratorPrerequisiteError)
        require_cluster(
            accelerator.kubeconfig_path, capability="an accelerator"
        )

        # Falsy, so an explicit {} lands here with an absent selector.
        # They are not the same thing to helm -- _values sends {} and
        # omits None -- but they are the same thing here: neither narrows
        # to a set of nodes this can check the label on.
        if not accelerator.node_selector:
            warnings.append(
                "no node_selector, so the DaemonSet is offered to every node "
                "the chart's affinity admits — on a cluster where one "
                "machine has the card, that is a pod per node for the sake "
                "of one. Name the GPU node to keep it where the hardware is."
            )
            return warnings

        nodes = self._nodes(accelerator)
        if not nodes:
            raise AcceleratorPrerequisiteError(
                f"node_selector {accelerator.node_selector} matches no node "
                "on this cluster. The release would install and its "
                "DaemonSet would schedule nothing, reporting DESIRED: 0 "
                "with no error and no event to explain it."
            )

        unlabelled = [
            node["metadata"]["name"] for node in nodes
            if NODE_FEATURE_LABEL not in (node["metadata"].get("labels") or {})
        ]
        if unlabelled:
            raise AcceleratorPrerequisiteError(
                f"{', '.join(unlabelled)} carries no {NODE_FEATURE_LABEL} "
                "label, which this chart's own affinity requires. Node "
                "Feature Discovery applies it on clusters that run NFD; "
                "this one does not, so it is applied by hand:\n"
                f"    kubectl label node {unlabelled[0]} "
                f"{NODE_FEATURE_LABEL}=true --overwrite\n"
                "Without it the release installs cleanly, the DaemonSet "
                "reports DESIRED: 0, `helm --wait` is satisfied because "
                "zero of zero pods are ready, and no GPU is ever "
                "allocatable. Labelling a node is a change to the machine "
                "rather than to a release, which is why this asks rather "
                "than doing it."
            )
        return warnings

    def create(self, accelerator: Accelerator) -> str:
        accelerator.validate()
        for warning in self.check_prerequisites(accelerator):
            print(f"[accelerator] warning: {warning}")

        options = accelerator.options
        self._helm(accelerator).install_or_upgrade(
            options.release_name,
            chart=options.chart,
            chart_version=options.chart_version,
            namespace=accelerator.resolved_namespace,
            values=self._values(accelerator),
            strict_values=True,
        )

        # The check that the script this replaces ended with, and for the
        # same reason: every failure mode above produces DESIRED: 0, and
        # helm reports success either way. A plugin scheduled nowhere has
        # installed nothing.
        raw = kubectl(
            accelerator.kubeconfig_path, "get", "daemonset",
            "-n", accelerator.resolved_namespace, "-l", DAEMONSET_SELECTOR,
            "-o", "json", error_cls=AcceleratorError, check=False,
        )
        desired = 0
        try:
            for item in json.loads(raw or "{}").get("items", []):
                desired += int(
                    (item.get("status") or {}).get("desiredNumberScheduled", 0)
                )
        except (json.JSONDecodeError, TypeError, ValueError):
            desired = 0
        if desired < 1:
            raise AcceleratorError(
                f"{options.release_name} installed, but its DaemonSet wants "
                "0 pods, so nothing advertises "
                f"{accelerator.resource_name} and a GPU workload will stay "
                "Pending. The affinity matched no node — check the "
                f"{NODE_FEATURE_LABEL} label and the node_selector."
            )
        return accelerator.resource_name

    def delete(self, accelerator: Accelerator) -> None:
        self._helm(accelerator).uninstall(
            accelerator.options.release_name,
            namespace=accelerator.resolved_namespace,
            missing_ok=True,
        )
