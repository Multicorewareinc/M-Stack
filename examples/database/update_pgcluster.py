"""
Update a PostgreSQL Cluster using the Multistack database SDK.

Prerequisite:
    The CloudNativePG operator must already be installed and the
    PostgreSQL Cluster must already exist.

This example updates only the PostgreSQL Cluster resource.
It does not install or update the CloudNativePG operator.

The test scales the PostgreSQL Cluster from 3 instances to 2.

    pip install -e .                                # from the repo root
    python3 examples/database/update_pgcluster.py

Requires `helm` and `kubectl` on PATH and a valid Kubernetes
kubeconfig.
"""

import os

from multistack import Database, DatabaseConfig
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

    # Scale the existing cluster from 3 instances to 2.
    instances=2,

    database=DatabaseConfig(
        name="appdb",
        owner="appuser",
        # Required to build the manifest's bootstrap block, and
        # ignored: CloudNativePG reads bootstrap.initdb only when it
        # first creates the cluster, so this value cannot change the
        # role's password. Rotating it is an ALTER ROLE, or CNPG's
        # spec.managed.roles -- not an update here.
        #
        # So this is a placeholder, not a credential, and deliberately
        # not prompted for: asking someone to type a password that
        # provably cannot take effect teaches them to type real ones
        # into prompts that do not matter. It does not read
        # CNPG_PASSWORD either, for the same reason -- setting that
        # variable here would look like a rotation and silently not be
        # one.
        password="unused-after-bootstrap",
    ),
)

cluster = DatabaseBackend().update_cluster(database)

print("\nCloudNativePG PostgreSQL Cluster updated successfully")
print(f"  name:       {cluster.name}")
print(f"  namespace:  {cluster.resolved_namespace}")
print(f"  instances:  {cluster.instances}")
print(f"  storage:    {cluster.storage_size}")
print(f"  database:   {cluster.database.name}")
print(f"  owner:      {cluster.database.owner}")
