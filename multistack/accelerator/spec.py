"""The accelerator capability: making a node's GPUs schedulable.

    Accelerator(kubeconfig_path=kc,
                node_selector={"kubernetes.io/hostname": "gpu-node"})

Kubernetes does not know a node has a GPU. Until something advertises
one as an extended resource, `nvidia.com/gpu` is not a thing a pod can
ask for -- so a GPU workload stays Pending, and a cluster with a healthy
card in it looks exactly like a cluster with none.

This capability installs the thing that does the advertising. It is the
one layer whose output is not an address: nothing connects to it, and
what it changes is what the scheduler believes about a node.

Only `nvidia_device_plugin` today, which is what this platform runs.
NVIDIA's GPU Operator and Tenstorrent's operator are reachable through
the Helm layer's own `AcceleratorManager` (see
multistack/helm/accelerator/) -- that predates this capability and still
works standalone. Either would become a second `type` here, and the
Operator is deliberately not the default: it also owns the driver, the
container toolkit and DCGM, so on a node whose drivers are already
installed it needs `driver.enabled=false` or it fights them. The device
plugin advertises the resource and nothing else.

No PROVIDES, and that is the design rather than an omission. Every other
capability publishes something a dependent fills a field from -- a URL, a
StorageClass name. This publishes a node property. `Inference` already
derives `nvidia.com/gpu` from its own `device="gpu"`, so a published
resource name would have no consumer, and the SDK's own invariant
(tests/test_stack.py) rightly refuses a published key that no capability
output maps to. `resource_name` is here as a plain property for a caller
and for the driver's own verification. If a spec ever needs to declare
`REQUIRES = ("accelerator",)`, that needs one line in
`CAPABILITY_OUTPUT` first -- without it `_check_order` silently skips
the requirement.
"""
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, field_validator

from ..capability import CapabilitySpec

SUPPORTED_TYPES: Tuple[str, ...] = ("nvidia_device_plugin",)

# The extended resource each implementation teaches the scheduler about.
# This is the string a pod puts in `resources.limits`.
RESOURCE_NAMES: Dict[str, str] = {
    "nvidia_device_plugin": "nvidia.com/gpu",
}

# Chart 0.20.0's own affinity requires one of the labels Node Feature
# Discovery applies. A cluster not running NFD has none of them, and the
# result is a DaemonSet with DESIRED: 0 -- no pod, no event, no error,
# and `helm --wait` satisfied because zero of zero pods are ready. This
# is the label the driver checks for, and the one an operator applies by
# hand when NFD is absent.
NODE_FEATURE_LABEL = "nvidia.com/gpu.present"


class DevicePluginOptions(BaseModel):
    """Deployment settings for NVIDIA's k8s-device-plugin."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    release_name: str = "nvdp"
    chart: str = "nvidia-device-plugin"
    # Pinned, not floating: a device plugin is the kind of thing nobody
    # looks at until GPUs stop being allocatable, and a silent minor bump
    # that changes the affinity is exactly how that happens.
    chart_version: Optional[str] = "0.20.0"

    # The plugin has to run under the nvidia runtime to see the card.
    # RKE2 ships the `nvidia`, `nvidia-experimental` and `crun`
    # RuntimeClasses via its rke2-runtimeclasses release, so the class
    # already exists here -- the plugin just has to ask for it. Without
    # this the pod starts and advertises nothing.
    runtime_class_name: str = "nvidia"

    @field_validator("release_name", "chart", "runtime_class_name")
    @classmethod
    def _not_empty(cls, value: str, info) -> str:
        if not value:
            raise ValueError(f"{info.field_name} must not be empty")
        return value

    def validate(self) -> None:
        self.model_validate(self.model_dump())


OPTIONS_FOR_TYPE = {"nvidia_device_plugin": DevicePluginOptions}
AcceleratorOptions = DevicePluginOptions


class Accelerator(CapabilitySpec):
    """Accelerator scheduling support on an existing Kubernetes cluster."""

    CAPABILITY: ClassVar[str] = "accelerator"
    SUPPORTED_TYPES: ClassVar[Tuple[str, ...]] = SUPPORTED_TYPES
    OPTIONS_FOR_TYPE: ClassVar[Dict[str, type]] = OPTIONS_FOR_TYPE
    # kube-system, matching where this platform's release lives. It is a
    # cluster-level extension rather than a workload, and it needs no
    # namespace of its own.
    DEFAULT_NAMESPACES: ClassVar[Dict[str, str]] = {
        "nvidia_device_plugin": "kube-system",
    }

    REQUIRES: ClassVar[Tuple[str, ...]] = ("cluster",)
    FROM_STACK: ClassVar[Dict[str, str]] = {
        "kubeconfig_path": "kubeconfig_path",
    }
    # PROVIDES is deliberately absent -- see the module docstring.

    type: str = "nvidia_device_plugin"
    kubeconfig_path: str
    namespace: Optional[str] = None
    options: Optional[AcceleratorOptions] = None

    # Which nodes run the plugin. Left None it runs wherever the chart's
    # affinity allows, which on a cluster with one GPU machine is a
    # DaemonSet pod on every node for the sake of one. Naming the node is
    # also what keeps this honest on a cluster where only some machines
    # have cards.
    node_selector: Optional[Dict[str, str]] = None
    tolerations: Optional[List[Dict[str, Any]]] = None

    @field_validator("kubeconfig_path")
    @classmethod
    def _kubeconfig_required(cls, value: str) -> str:
        if not value:
            raise ValueError(
                "kubeconfig_path is required — this deploys into an existing "
                "cluster, and the SDK won't fall back to ambient "
                "$KUBECONFIG/~/.kube/config, which can silently target the "
                "wrong cluster."
            )
        return value

    def validate(self) -> None:
        self.validate_capability()

    @property
    def release_name(self) -> str:
        return self.options.release_name if self.options else "nvdp"

    @property
    def resource_name(self) -> str:
        """The extended resource a pod asks for once this is installed.

        Not published to the stack (see the module docstring), but it is
        what the driver checks a node became able to offer, and what a
        caller quotes rather than typing `nvidia.com/gpu` out.
        """
        return RESOURCE_NAMES[self.type]
