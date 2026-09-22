"""
Make a node's GPUs schedulable, with the Multistack SDK.

Kubernetes does not know a node has a GPU. Until something advertises
one as an extended resource, `nvidia.com/gpu` is not a thing a pod can
ask for — so a GPU workload stays Pending on a machine with a perfectly
healthy card in it. This installs the device plugin that advertises it.

    pip install -e ".[helm]"                  # from the repo root
    MULTISTACK_GPU_NODE=192.0.2.20 python3 examples/accelerator/install.py

Requires `helm` and `kubectl` on PATH and a cluster that already exists.
Unlike most capabilities here this one also changes a node: see the label
below.
"""

import os

from multistack.accelerator import (
    Accelerator,
    AcceleratorBackend,
    NODE_FEATURE_LABEL,
)
from multistack.kube import kubectl, node_name_for


# Not /tmp: that does not survive a reboot, and losing the kubeconfig
# leaves you with a cluster you cannot reach and no obvious reason why.
KUBECONFIG = os.path.expanduser(
    os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig")
)

# The machine with the card, by IP — the SDK addresses nodes by IP,
# because that is what you can reach before a cluster exists. 192.0.2.20
# is an RFC 5737 documentation address, so the default cannot be mistaken
# for a real host.
GPU_NODE = os.environ.get("MULTISTACK_GPU_NODE", "192.0.2.20")

node = node_name_for(KUBECONFIG, GPU_NODE)

# The chart's own affinity requires a label that Node Feature Discovery
# applies on clusters that run it. On a cluster that does not, the label
# has to be applied by hand — and without it the release installs
# cleanly, its DaemonSet reports DESIRED: 0, `helm --wait` is satisfied
# because zero of zero pods are ready, and no GPU is ever allocatable.
#
# It is here rather than inside the capability because labelling a node
# is a change to the machine, not to a release. The driver checks for it
# and refuses rather than installing something inert, so leaving this out
# is an error with an explanation, not a silent no-op.
kubectl(KUBECONFIG, "label", "node", node,
        f"{NODE_FEATURE_LABEL}=true", "--overwrite")

accelerator = Accelerator(
    # Required — no ambient $KUBECONFIG fallback, so this can only ever
    # act on the cluster you explicitly specify.
    kubeconfig_path=KUBECONFIG,

    # Pinned to the machine that has the card. Left unset, the DaemonSet
    # is offered to every node the chart's affinity admits, which on a
    # cluster with one GPU machine is a pod per node for the sake of one.
    node_selector={"kubernetes.io/hostname": node},

    # namespace omitted: kube-system is the default, and this is a
    # cluster-level extension rather than a workload, so it needs no
    # namespace of its own.
)

backend = AcceleratorBackend()
for warning in backend.check_prerequisites(accelerator):
    print(f"[accelerator] warning: {warning}")

resource = backend.create(accelerator)

print("\nAccelerator support installed")
print(f"  node:       {node}")
print(f"  release:    {accelerator.release_name}")
print(f"  namespace:  {accelerator.resolved_namespace}")
print(f"  resource:   {resource}")
print(f"\n  verify:     kubectl --kubeconfig {KUBECONFIG} get node {node} \\")
print(f"                -o jsonpath='{{.status.allocatable.{resource}}}'")
