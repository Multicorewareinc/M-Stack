"""
Add or remove nodes on a running RKE2 cluster with the Multistack SDK.

There is no "add node" or "remove node" call. You declare the node set you
want and `RKE2Backend.update()` diffs it against what it provisioned:
nodes in the spec but not deployed get joined, nodes deployed but not in
the spec get RKE2 uninstalled and dropped.

This example drops the second agent that `examples/rke2/cluster.py`
created. Adding is the same operation from the other direction — put the
node back in `nodes` (or add a new one) and run it again.

    pip install -e .                          # from the repo root
    python3 examples/rke2/update.py

Needs a cluster this SDK created — `update()` reads its own state from
`~/.multistack/state/` to know what's currently deployed, and raises
`RKE2Error` if there's none. Run `examples/rke2/cluster.py` first.

Destructive: RKE2 is uninstalled from any node you drop, so anything
running there goes with it.
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
    # Must match the version that's currently deployed. update() compares
    # this against its saved state and upgrades every existing node when
    # they differ — so a stale value here would quietly bundle a version
    # change into what you meant to be a node-only change. Use
    # examples/rke2/upgrade.py when you want the version to move.
    version="v1.32.5+rke2r1",
    # 192.0.2.12 is absent from this list on purpose — that's what tells
    # update() to drop it. The first server can't be removed this way;
    # delete() and create() a new cluster if you need to replace it.
    nodes=[
        RKE2Node(address=SERVER, user=SSH_USER, role="server", ssh_key=SSH_KEY),
        *[RKE2Node(address=a, user=SSH_USER, role="agent", ssh_key=SSH_KEY)
          for a in AGENTS],
    ],
    kubeconfig_path=KUBECONFIG,
    # Both have to match what the cluster was created with — RKE2 can't
    # swap a CNI on a running cluster, and kube-proxy replacement has to be
    # staged before the first server ever starts, so update() rejects a
    # change to either.
    cni="cilium",
    disable_kube_proxy=True,
)

backend = RKE2Backend()

kubeconfig = backend.update(cluster)

print(f"\nCluster '{cluster.name}' reconciled to {len(cluster.nodes)} node(s)")
print(f"Kubeconfig: {cluster.kubeconfig_path}")
print(f"Try: KUBECONFIG={cluster.kubeconfig_path} kubectl get nodes")
