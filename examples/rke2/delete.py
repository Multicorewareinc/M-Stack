"""
Tear down an RKE2 cluster with the Multistack SDK.

`RKE2Backend.delete()` takes just the cluster name — after this there's no
cluster left for a spec to describe. It uninstalls RKE2 from every node it
recorded (agents in parallel, then any server past the first, then the
first server last), removes the kubeconfig it wrote, and deletes its own
state file.

Runs Vault's own teardown first, while the cluster can still be reached:
RKE2Backend knows nothing of Vault (or anything else built on top of it —
capabilities depend upward on "cluster", never the other way around), so
without this step destroying the cluster silently orphans Vault's local
init file (`~/.multistack/vault/<release>-init.json`) — the PVC it
describes is gone the moment the cluster is, but the file survives on this
machine and blocks the next `create()` (it refuses to mint a new keyring
under a file that looks like it already has one). `VaultBackend.delete()`
removes the release, the PVC and that file together, which is the only
combination that doesn't strand one without the other. Best-effort and
side-stepped entirely when there's no kubeconfig to reach a cluster with,
the same "half-finished create() can still be tidied up" spirit as the
RKE2 teardown below.

    pip install -e .                          # from the repo root
    python3 examples/rke2/delete.py

Needs a cluster this SDK created — the node list comes from its own state
in `~/.multistack/state/`, so it raises `RKE2Error` if there's nothing
recorded under this name. A cluster provisioned some other way is invisible
to it.

Destructive and not reversible: this uninstalls RKE2 from every node in the
cluster, and discards Vault's PVC and unseal keys along with it.
"""

import os

from multistack import Vault, VaultBackend, VaultOptions
from multistack.backends.rke2_client import RKE2Backend

CLUSTER_NAME = "ai-cluster"

# Same default every example in this repo uses (full_stack.py,
# examples/vault/install.py, ...) — not read from RKE2's own state, so
# this step still runs even against a cluster this process didn't create.
KUBECONFIG = os.path.expanduser(os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig"))

if os.path.exists(KUBECONFIG):
    try:
        vault = Vault(kubeconfig_path=KUBECONFIG, options=VaultOptions())
        VaultBackend().delete(vault, delete_data=True)
    except Exception as exc:
        # Best-effort: a cluster with no Vault release ever installed, or
        # one already unreachable, should not block the RKE2 teardown
        # below — see this file's own module docstring.
        print(f"[teardown] vault cleanup skipped: {exc}")
else:
    print(f"[teardown] no kubeconfig at {KUBECONFIG} — skipping Vault cleanup")

backend = RKE2Backend()

backend.delete(CLUSTER_NAME)

print(f"\nCluster '{CLUSTER_NAME}' deleted, nodes uninstalled, state removed")
