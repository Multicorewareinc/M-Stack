"""
Tear down an RKE2 cluster with the Multistack SDK.

`RKE2Backend.delete()` takes just the cluster name — after this there's no
cluster left for a spec to describe. It uninstalls RKE2 from every node it
recorded (agents in parallel, then any server past the first, then the
first server last), removes the kubeconfig it wrote, and deletes its own
state file.

Best-effort by design: a node that's unreachable or already clean won't
block cleanup of the rest, so a half-finished create() can still be tidied
up with this.

    pip install -e .                          # from the repo root
    python3 examples/rke2/delete.py

Needs a cluster this SDK created — the node list comes from its own state
in `~/.multistack/state/`, so it raises `RKE2Error` if there's nothing
recorded under this name. A cluster provisioned some other way is invisible
to it.

Destructive and not reversible: this uninstalls RKE2 from every node in the
cluster.
"""
from multistack.backends.rke2_client import RKE2Backend

CLUSTER_NAME = "ai-cluster"

backend = RKE2Backend()

backend.delete(CLUSTER_NAME)

print(f"\nCluster '{CLUSTER_NAME}' deleted, nodes uninstalled, state removed")
