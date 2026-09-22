"""
Delete a Valkey deployment with the Multistack SDK.

This example performs a full teardown:
- Uninstalls the Valkey Helm release.
- Deletes persistent volume claims associated with the release.
- Deletes the Kubernetes namespace.

    pip install -e .                          # from the repo root
    python3 examples/cache/delete.py

Needs `helm` and `kubectl` on PATH, a cluster kubeconfig, and an
existing Valkey deployment.
"""

import os

from multistack import Cache
from multistack.cache import CacheBackend, ValkeyOptions

# Not /tmp: that does not survive a reboot, and losing the kubeconfig
# leaves you with a cluster you cannot reach and no obvious reason why.
KUBECONFIG = os.path.expanduser(
    os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig")
)



cache = Cache(
    # Target the same cluster used to create the deployment.
    kubeconfig_path=KUBECONFIG,

    # Existing Helm release identity.
    name="valkey-test",
    namespace="valkey-test",

    options=ValkeyOptions(chart="valkey"),
)

backend = CacheBackend()

# Full teardown:
#   1. Uninstall the Helm release.
#   2. Delete the Valkey PVCs.
#   3. Delete the Valkey namespace.
backend.delete(
    cache,
    delete_pvcs=True,
    delete_namespace=True,
)