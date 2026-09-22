"""
Install Longhorn distributed block storage on an RKE2 cluster.

RKE2 ships with no storage provisioner and no StorageClass, so a fresh
cluster can't satisfy a PVC at all — anything that asks for storage sits
Pending indefinitely. Longhorn fills that gap, and everything downstream
that needs persistence (MinIO tenants, model caches) binds against the
StorageClass it creates.

    pip install -e .                          # from the repo root
    python3 examples/storage/install.py

Needs `helm`, `kubectl` and `ssh` on PATH, a cluster whose kubeconfig is
below, and passwordless key-based SSH plus passwordless sudo on every node
(same requirements as the RKE2 backend).

Two-step by design: node packages are installed by an explicit call, since
that mutates the nodes. Comment out install_prerequisites() once the nodes
are already prepared — check_prerequisites() inside create() will confirm.
"""
from multistack import RKE2Node, Storage, StorageBackend
from multistack.storage import LonghornOptions
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

# EVERY node in the cluster, not a convenient subset. Longhorn's node
# components are a DaemonSet: they run on every schedulable node whether or
# not it appears here, and a node without open-iscsi crash-loops its
# longhorn-manager and leaves Longhorn degraded. Nodes reached with a
# different user or key still belong in this list — create() cross-checks
# against the cluster's actual node list and warns about any it can't see.
nodes = [
    RKE2Node(address=SERVER, user=SSH_USER, role="server", ssh_key=SSH_KEY),
    *[RKE2Node(address=a, user=SSH_USER, role="agent", ssh_key=SSH_KEY)
      for a in AGENTS],
]

# A pydantic model: this call validates. A bad `type`, a relative
# `data_path`, a misspelled field name — all fail on the line below rather
# than part-way through a Helm install. `Storage.model_validate(data)`
# builds the same spec from a dict, which is how one would come from YAML.
storage = Storage(
    # The implementation. Required and explicit rather than defaulted, so
    # the choice is always visible in the file that makes it.
    type="longhorn",

    namespace="test-longhorn",  # optional; defaults to `longhorn-system`
    # Required. Points at exactly one cluster — no ambient $KUBECONFIG
    # fallback, so this can't silently install into the wrong place.
    kubeconfig_path=KUBECONFIG,
    # Longhorn's default is 3, which needs 3 schedulable nodes. On a
    # smaller cluster lower this, or volumes stay Degraded forever.
    replica_count=3,
    # Makes `longhorn` the cluster's default StorageClass, so a PVC that
    # names no storageClassName still binds.
    default_storage_class=True,
    # Pin for reproducibility: unpinned, two runs a month apart can install
    # different Longhorn versions from this same file.
    # chart_version="1.9.1",

    # Everything above means the same thing to any implementation. Below
    # is Longhorn's own, on a typed object that has to match `type` — so a
    # Longhorn setting can't be handed to a different implementation and
    # quietly do nothing.
    # Leave `options` out entirely and these defaults are filled in.
    options=LonghornOptions(
        # Where replica data lives on each node. Point it at a dedicated
        # disk mount if you have one; the default sits on the root volume,
        # which is fine for a lab and not for anything real.
        data_path="/var/lib/longhorn",

        # Pinned deliberately. Longhorn's auto-detection reads kubelet's
        # command line via pod logs, which fails whenever the API server
        # can't reach a node — and then the CSI driver never deploys at
        # all, with nothing in the Longhorn UI to say why. Change this
        # only if your kubelet genuinely runs elsewhere (k0s and some
        # managed distros do).
        kubelet_root_dir="/var/lib/kubelet",

        # RWX (ReadWriteMany) volumes are served over NFSv4, so every node
        # needs an NFS client. install_prerequisites() below puts it there.
        # Set False if RWO is enough and you'd rather not install
        # nfs-common — MinIO, Postgres and most single-writer workloads
        # never need RWX.
        enable_rwx=True,

        # The Helm release name, which is also the StorageClass name
        # Longhorn creates. Changing it changes what your PVCs must name.
        release_name="longhorn",
    ),
)

backend = StorageBackend()

# Mutates the nodes: installs open-iscsi + nfs-common and enables iscsid.
# Safe to re-run; drop it once the nodes are prepared.
backend.install_prerequisites(storage, nodes)

# Checks prerequisites, then installs the chart and waits for it to be Ready.
storage_class = backend.create(storage, nodes=nodes)

print(f"\nLonghorn installed — StorageClass: {storage_class}")
print(f"Try: KUBECONFIG={storage.kubeconfig_path} kubectl get sc")
print(f"     KUBECONFIG={storage.kubeconfig_path} kubectl -n {storage.resolved_namespace} get pods")
