"""
Manual verification script for RKE2Backend's create/update/delete + state
tracking. Run each stage separately so you can inspect the machine between
steps — this is meant to be run interactively, not as an automated test.

Usage:
    python3 examples/rke2/verify.py create
    python3 examples/rke2/verify.py update
    python3 examples/rke2/verify.py upgrade
    python3 examples/rke2/verify.py delete

DESTRUCTIVE, and it targets the same machines as the real cluster. RKE2
allows only one installation per host, so `create` here uninstalls
whatever ai-cluster put on those nodes, even though this uses a different
cluster name and kubeconfig. Run it only when you are willing to rebuild
with examples/full_stack.py afterwards.

`update` exercises the add-node diff by leaving EXTRA_NODE out of the
create set and adding it back. The backend identifies nodes by address, so
no two entries may share one — RKE2Cluster refuses duplicates when it is
constructed, so a bad node set fails here rather than mid-provision.
"""
import sys

from multistack import RKE2Cluster, RKE2Node
from multistack.backends.rke2_client import RKE2Backend
import os

CLUSTER_NAME = "verify-cluster"
KUBECONFIG_PATH = "/tmp/verify-cluster-kubeconfig.yaml"

OLD_VERSION = "v1.32.5+rke2r1"
NEW_VERSION = "v1.33.1+rke2r1"

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

# Held back from the create set so `update` has a real node to join, so
# it must not be one of AGENTS -- adding a node already in the cluster
# reconciles to a no-op and the example demonstrates nothing.
EXTRA_NODE = RKE2Node(
    address=os.environ.get("MULTISTACK_EXTRA_NODE", "192.0.2.14"),
    user=SSH_USER, role="agent", ssh_key=SSH_KEY,
)

backend = RKE2Backend(poll_timeout=120)


def build_cluster(version: str, extra_node: bool = False) -> RKE2Cluster:
    nodes = [
        RKE2Node(address=SERVER, user=SSH_USER, role="server", ssh_key=SSH_KEY),
        *[RKE2Node(address=a, user=SSH_USER, role="agent", ssh_key=SSH_KEY)
          for a in AGENTS],
    ]
    if extra_node:
        nodes.append(EXTRA_NODE)
    return RKE2Cluster(
        name=CLUSTER_NAME,
        version=version,
        nodes=nodes,
        kubeconfig_path=KUBECONFIG_PATH,
        disable_kube_proxy=True,
        cni="cilium",
    )


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "create"

    if action == "create":
        # Deliberately create on an older pinned version so `upgrade` below
        # has something real to bump.
        kubeconfig = backend.create(build_cluster(OLD_VERSION))
        print("\n--- kubeconfig ---")
        print(kubeconfig)

    elif action == "update":
        # Joins EXTRA_NODE. Version stays the same as create() here — this
        # call is purely about exercising the add-node diff, not upgrade.
        kubeconfig = backend.update(build_cluster(OLD_VERSION, extra_node=True))
        print("\n--- kubeconfig ---")
        print(kubeconfig)

    elif action == "upgrade":
        # Same node set as create(), only the version changes — this
        # exercises update()'s upgrade path specifically, with no
        # add/remove node diffing involved. Run this before `update`, or
        # it will uninstall the node `update` just joined.
        kubeconfig = backend.update(build_cluster(NEW_VERSION))
        print("\n--- kubeconfig ---")
        print(kubeconfig)

    elif action == "delete":
        backend.delete(CLUSTER_NAME)
        print(f"Deleted cluster '{CLUSTER_NAME}' and its state file.")

    else:
        print(f"Unknown action: {action}. Use create, update, upgrade, or delete.")