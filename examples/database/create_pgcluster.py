"""
Create a PostgreSQL Cluster using the Multistack database SDK.

Prerequisite:
    The CloudNativePG operator must already be installed
    (examples/database/create_operator.py).

This example creates only the PostgreSQL Cluster resources.
It does not install or update the CloudNativePG operator.

    pip install -e .                                # from the repo root
    python3 examples/database/create_pgcluster.py

Requires `helm` and `kubectl` on PATH and a valid Kubernetes
kubeconfig.
"""

import os

from multistack import Database, DatabaseConfig, prompt_secret
from multistack.database import DatabaseBackend


# Not /tmp: that does not survive a reboot, and losing the kubeconfig
# leaves you with a cluster you cannot reach and no obvious reason why.
KUBECONFIG = os.path.expanduser(
    os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig")
)

# Cluster identity and the database it bootstraps, overridable so this
# script can create a real platform database as well as the demo it
# defaults to. Defaults stay `cnpg-test` for the same reason the cache
# example keeps `valkey-test`.
NAME = os.environ.get("MULTISTACK_CNPG_NAME", "cnpg-test")
# Unset means "use the implementation's own default" (`postgres`, from
# Database.DEFAULT_NAMESPACES) rather than a second, different default
# living here. An example that defaults somewhere the SDK does not is an
# example that quietly teaches the wrong layout.
NAMESPACE = os.environ.get("MULTISTACK_CNPG_NS") or None
DB_NAME = os.environ.get("MULTISTACK_CNPG_DB", "appdb")
DB_OWNER = os.environ.get("MULTISTACK_CNPG_OWNER", "appuser")
INSTANCES = int(os.environ.get("MULTISTACK_CNPG_INSTANCES", "3"))

database = Database(
    # Required — no ambient $KUBECONFIG fallback, so this can only ever
    # act on the cluster you explicitly specify.
    kubeconfig_path=KUBECONFIG,

    # PostgreSQL Cluster configuration. No `options` here: this half of
    # the capability is the same shape whoever runs it, so it lives on
    # the spec rather than in the implementation's options.
    name=NAME,
    # Omitted when unset, so the spec's own default applies.
    **({"namespace": NAMESPACE} if NAMESPACE else {}),
    instances=INSTANCES,

    database=DatabaseConfig(
        name=DB_NAME,
        owner=DB_OWNER,
        # Asked for, not written down. This is the one credential that
        # has to originate here -- CloudNativePG reads
        # bootstrap.initdb.secret only at bootstrap, so this value is
        # what the role is created with, and the driver hands it
        # straight to the Secret below. Everything afterwards reads it
        # from there, which is why `cluster.endpoint` carries no
        # password.
        #
        # confirm=True because this is the credential being *set*: a
        # typo here is a role nobody can log in as, not an error.
        password=prompt_secret(
            f"PostgreSQL password for role {DB_OWNER!r}",
            env_var="CNPG_PASSWORD",
            confirm=True,
        ),
    ),
)

cluster = DatabaseBackend().create_cluster(database)

print("\nCloudNativePG PostgreSQL Cluster created successfully")
print(f"  name:       {cluster.name}")
print(f"  namespace:  {cluster.resolved_namespace}")
print(f"  instances:  {cluster.instances}")
print(f"  storage:    {cluster.storage_size}")
print(f"  database:   {cluster.database.name}")
print(f"  owner:      {cluster.database.owner}")
print(f"  url:        {cluster.endpoint}")
print(f"\n  password:   kubectl -n {cluster.resolved_namespace} get secret "
      f"{cluster.secret_name} \\")
print("                -o jsonpath='{.data.password}' | base64 -d")
