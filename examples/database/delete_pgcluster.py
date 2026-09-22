"""
Delete a PostgreSQL Cluster using the Multistack database SDK.

Prerequisite:
    The CloudNativePG operator must already be installed and the
    PostgreSQL Cluster should already exist.

This example deletes only the PostgreSQL Cluster resources.
It does not uninstall or modify the CloudNativePG operator.

By default, delete_cluster() removes:
    - PostgreSQL Cluster custom resource
    - PostgreSQL bootstrap Secret

It does not delete:
    - CloudNativePG operator
    - CloudNativePG CRDs
    - Operator namespace

    pip install -e .                                # from the repo root
    python3 examples/database/delete_pgcluster.py

Requires `helm` and `kubectl` on PATH and a valid Kubernetes
kubeconfig.
"""

import os

from multistack import Database
from multistack.database import DatabaseBackend


# Not /tmp: that does not survive a reboot, and losing the kubeconfig
# leaves you with a cluster you cannot reach and no obvious reason why.
KUBECONFIG = os.path.expanduser(
    os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig")
)

database = Database(
    # Required — no ambient $KUBECONFIG fallback, so this can only ever
    # act on the cluster you explicitly specify.
    kubeconfig_path=KUBECONFIG,

    # Existing PostgreSQL Cluster configuration.
    name="cnpg-test",
    namespace="cnpg-test",

    # Keep the existing Cluster configuration consistent.
    instances=2,

    # No database block. Removing a cluster needs its name and
    # namespace, not its credentials -- validate_cluster_identity()
    # exists so that deleting one does not mean typing its password
    # into a spec first.
)

DatabaseBackend().delete_cluster(database)

print("\nCloudNativePG PostgreSQL Cluster deleted successfully")
print(f"  name:       {database.name}")
print(f"  namespace:  {database.resolved_namespace}")
