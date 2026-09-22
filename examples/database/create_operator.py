"""
Install the CloudNativePG operator using the Multistack SDK.

This example installs only the CloudNativePG operator on an existing
Kubernetes cluster using the common Helm integration layer.

It does not create any PostgreSQL Cluster custom resources.

    pip install -e .                              # from the repo root
    python3 examples/database/create_operator.py

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

# The chart to install. A name resolves through HelmRunner's repository
# list; a path or a .tgz installs from disk, which is what a mirror, a
# proxy or an air-gapped host needs -- and what to use when the upstream
# CDN is slow enough to time out mid-install:
#
#   helm pull cloudnative-pg --repo https://cloudnative-pg.github.io/charts \
#     --version 0.29.0 -d /tmp
#   MULTISTACK_CNPG_CHART=/tmp/cloudnative-pg-0.29.0.tgz python3 <this>
CHART = os.environ.get("MULTISTACK_CNPG_CHART", "cloudnative-pg")
# A local .tgz carries its own version, and passing one alongside it makes
# helm look for that version *inside* the file and fail.
CHART_VERSION = (None if CHART.endswith(".tgz")
                 else os.environ.get("MULTISTACK_CNPG_CHART_VERSION", "0.29.0"))

database = Database(
    # Required — no ambient $KUBECONFIG fallback, so this can only ever
    # act on the cluster you explicitly specify.
    kubeconfig_path=KUBECONFIG,

    # `cnpg` is the default and the only implementation today; naming it
    # is what makes swapping in another one a one-word change here
    # rather than a rewrite.
    type="cnpg",

    # The operator's own Helm release. It lives in the implementation's
    # options because every field of it is CloudNativePG-specific.
    options=CNPGOptions(
        operator_release_name="cnpg",
        operator_namespace="cnpg-system",
        operator_chart=CHART,
        operator_chart_version=CHART_VERSION,
    ),
)

release = DatabaseBackend().create(database)

print("\nCloudNativePG operator deployed successfully")
print(f"  release:   {release.name}")
print(f"  namespace: {release.namespace}")
print(f"  revision:  {release.revision}")
print(f"  status:    {release.status.value}")
print(f"  chart:     {release.chart_name}")
print(f"  version:   {release.chart_version}")
