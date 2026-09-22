"""
Update the CloudNativePG operator using the Multistack SDK.

It does not modify any PostgreSQL Cluster custom resources.

The test overrides the operator replicaCount from the chart default
of 1 to 2.

    pip install -e .                              # from the repo root
    python3 examples/database/update_operator.py

Requires `helm` and `kubectl` on PATH and a valid Kubernetes
kubeconfig.
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
        # Raw chart values, for anything the options model does not
        # name directly.
        values={
            "replicaCount": 2,
        },
    ),
)

release = DatabaseBackend().update(database)

print("\nCloudNativePG operator updated successfully")
print(f"  release:   {release.name}")
print(f"  namespace: {release.namespace}")
print(f"  revision:  {release.revision}")
print(f"  status:    {release.status.value}")
print(f"  chart:     {release.chart_name}")
print(f"  version:   {release.chart_version}")
