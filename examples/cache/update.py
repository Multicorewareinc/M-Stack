"""
Update an existing Valkey deployment with the Multistack SDK.

The update uses deep-merge semantics by default. Only the supplied
values are changed; existing Valkey values are preserved.

    pip install -e .                          # from the repo root
    python3 examples/cache/update.py

Needs `helm` and `kubectl` on PATH and an existing Valkey Helm release.
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
    # Required — target the same cluster used to create the release.
    kubeconfig_path=KUBECONFIG,

    # Existing Helm release identity.
    name="valkey-test",
    namespace="valkey-test",

    # Chart and values are Bitnami-chart specifics, so they live on the
    # implementation's options rather than on the spec.
    options=ValkeyOptions(
        chart="valkey",

        # Current desired configuration.
        values={
            "architecture": "standalone",

            # The same auth Secret the release was created with (see
            # create.py). Naming it again rather than the password keeps
            # the credential out of this spec too -- an update deep-merges
            # into the live values, so a password written here would be
            # written into the release.
            "auth": {
                "enabled": True,
                "existingSecret": "valkey-auth",
                "existingSecretPasswordKey": "valkey-password",
            },

            "primary": {
                "persistence": {
                    "enabled": True,
                    "size": "10Gi",
                },
            },
        },
    ),
)

backend = CacheBackend()

# Change only the replicaCount.
# The remaining Helm values are preserved through deep merge.
release = backend.update(
    cache,
    values={
        "architecture": "replication",
        "replica": {
            "replicaCount": 2,
        },
    },
)

print(f"\nValkey release {release.name} updated successfully")
print(f"  namespace: {release.namespace}")
print(f"  status:    {release.status.value}")
print(f"  revision:  {release.revision}")
print(f"  chart:     {release.chart_name}")
print(f"  version:   {release.chart_version}")