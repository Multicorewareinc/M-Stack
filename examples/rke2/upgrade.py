"""
Upgrade a running RKE2 cluster's version with the Multistack SDK.

This is `RKE2Backend.update()` again — the same call that adds and removes
nodes. Leave the node set exactly as deployed and change only `version`,
and update() takes its upgrade path with no node diffing involved.

Nodes are upgraded in groups: agents in parallel, then any server past the
first, then the first server last, so the control plane stays reachable as
long as possible. This is not a zero-downtime rolling upgrade — expect the
API to blip.

    pip install -e .                          # from the repo root
    python3 examples/rke2/upgrade.py

Needs a cluster this SDK created — `update()` reads its own state from
`~/.multistack/state/` to know the current version, and raises `RKE2Error`
if there's none. Run `examples/rke2/cluster.py` first.

Only upgrades: RKE2 tracks upstream Kubernetes, which doesn't support
rolling a control plane backwards, so pointing `version` at an older
release is a good way to break a cluster rather than a supported downgrade.
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
    # The only thing that changes from examples/rke2/cluster.py, which
    # pinned v1.32.5+rke2r1. update() sees the difference against its saved
    # state and upgrades every node it already has.
    version="v1.33.1+rke2r1",
    # Has to be the set that's currently deployed for this to be purely an
    # upgrade. update() diffs node sets on every call, so a node missing
    # here gets uninstalled and an extra one gets joined, bundled into the
    # same operation as the version bump. This matches what
    # examples/rke2/cluster.py creates — if you also ran
    # examples/rke2/update.py (which drops 192.0.2.12), drop it here too,
    # or this call will re-join it on the way through.
    nodes=[
        RKE2Node(address=SERVER, user=SSH_USER, role="server", ssh_key=SSH_KEY),
        *[RKE2Node(address=a, user=SSH_USER, role="agent", ssh_key=SSH_KEY)
          for a in AGENTS],
    ],
    kubeconfig_path=KUBECONFIG,
    # Unchanged from creation — update() rejects a change to either.
    cni="cilium",
    disable_kube_proxy=True,
)

backend = RKE2Backend()

kubeconfig = backend.update(cluster)

print(f"\nCluster '{cluster.name}' upgraded to {cluster.version}")
print(f"Kubeconfig: {cluster.kubeconfig_path}")
print(f"Try: KUBECONFIG={cluster.kubeconfig_path} kubectl get nodes")
