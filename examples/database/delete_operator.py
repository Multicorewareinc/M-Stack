"""
Uninstall the CloudNativePG operator using the Multistack SDK.

It does not delete any PostgreSQL Cluster custom resources.

    pip install -e .                              # from the repo root
    python3 examples/database/delete_operator.py

Requires `helm` and `kubectl` on PATH and a valid Kubernetes
kubeconfig.

IMPORTANT:
    Delete all PostgreSQL Cluster resources managed by CloudNativePG
    before uninstalling the operator.

    Use DatabaseBackend.delete_cluster(database) to delete PostgreSQL
    clusters first, and only then run this operator delete example.

    This example removes only the CloudNativePG operator Helm release.
    It does not delete PostgreSQL Cluster custom resources.
"""

import os

from multistack import Database
from multistack.database import CNPGOptions, DatabaseBackend


# Not /tmp: that does not survive a reboot, and losing the kubeconfig
# leaves you with a cluster you cannot reach and no obvious reason why.
KUBECONFIG = os.path.expanduser(
    os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig")
)

database = Database(
    kubeconfig_path=KUBECONFIG,
    options=CNPGOptions(
        operator_release_name="cnpg",
        operator_namespace="cnpg-system",
        operator_chart="cloudnative-pg",
        operator_chart_version="0.29.0",
    ),
)

DatabaseBackend().delete(database)

print("\nCloudNativePG operator uninstalled successfully")
print(f"  release:   {database.operator_release_name}")
print(f"  namespace: {database.operator_namespace}")
