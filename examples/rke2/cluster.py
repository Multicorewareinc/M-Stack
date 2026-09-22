"""
Create an RKE2 cluster with the Multistack SDK.

Declare the cluster you want, hand it to `RKE2Backend.create()`, and get a
kubeconfig back. The backend installs RKE2 on the server node, joins the
agents, and writes the kubeconfig to `kubeconfig_path`.

    pip install -e .                          # from the repo root
    python3 examples/rke2/cluster.py

This installs system packages and systemd units on real machines over SSH,
so point NODES at hosts you're willing to have reformatted. Needs
passwordless key-based SSH to each node, `ssh` on your PATH, and a user
that is root or has non-interactive `sudo -n`.

To modify or tear down the cluster afterwards, the same backend takes
`update(cluster)` (reconciles nodes/version against what's deployed) and
`delete(cluster.name)`. `examples/rke2/verify.py` walks through those
stage by stage.
"""
from multistack import RKE2Cluster, RKE2Node
from multistack.backends.rke2_client import RKE2Backend
import os

# Your lab, from the environment, so this file needs no editing. The
# defaults are RFC 5737 documentation addresses and a documentation user:
# that range is reserved for exactly this purpose, so nothing here can be
# mistaken for a real host.
SSH_USER = os.environ.get("MULTISTACK_SSH_USER", "ubuntu")
SSH_KEY = os.environ.get("MULTISTACK_SSH_KEY", "~/.ssh/id_ed25519")

# Not /tmp: that does not survive a reboot, and losing the kubeconfig
# leaves you with a cluster you cannot reach and no obvious reason why.
KUBECONFIG = os.path.expanduser(
    os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig")
)
# The machines. One server, any number of agents, comma-separated.
SERVER = os.environ.get("MULTISTACK_SERVER", "192.0.2.10")
AGENTS = [a.strip() for a in os.environ.get(
    "MULTISTACK_AGENTS", "192.0.2.11,192.0.2.12,192.0.2.13").split(",") if a.strip()]

cluster = RKE2Cluster(
    name="ai-cluster",
    # Pinned one minor behind the latest so examples/rke2/upgrade.py has a
    # real bump to make. For a cluster you actually intend to keep, pin
    # whichever release you want to run.
    version="v1.32.5+rke2r1",
    nodes=[
        RKE2Node(address=SERVER, user=SSH_USER, role="server", ssh_key=SSH_KEY),
        *[RKE2Node(address=a, user=SSH_USER, role="agent", ssh_key=SSH_KEY)
          for a in AGENTS],
    ],
    kubeconfig_path=KUBECONFIG,
    # Cilium with its eBPF kube-proxy replacement. Both are fixed at
    # creation time — RKE2 can't swap a CNI on a running cluster, and the
    # kube-proxy HelmChartConfig has to be staged before the first server
    # ever starts.
    cni="cilium",
    disable_kube_proxy=True,

    # Every node in this lab is multi-homed. Without this, RKE2 picks
    # whichever address the kubelet fancies — which may be a network the
    # other nodes can't route to, and then the supervisor tunnel never
    # comes up. create() warns when it sees a second interface and this is
    # off; take the warning seriously.
    pin_node_ip=True,

    # Cilium defaults to an MTU of 1450. The measured path MTU here is
    # 1428, so 1428 - 50 (VXLAN overhead) = 1378, and 1350 is what the
    # working cluster ran. Get this wrong and pod traffic to the affected
    # node fails in ways that look like anything except MTU — this cost a
    # day to find. Fixed at creation time along with the CNI.
    cilium_mtu=1350,
)

backend = RKE2Backend()

kubeconfig = backend.create(cluster)

print(f"\nCluster '{cluster.name}' created with {len(cluster.nodes)} node(s)")
print(f"Kubeconfig: {cluster.kubeconfig_path}")
print(f"Try: KUBECONFIG={cluster.kubeconfig_path} kubectl get nodes")
