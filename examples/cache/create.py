"""
Deploy Valkey with the Multistack SDK.

Valkey provides an in-memory key-value store on an existing Kubernetes
cluster through the Bitnami Valkey Helm chart.

    pip install -e .                          # from the repo root
    python3 examples/cache/create.py

Needs `helm` and `kubectl` on PATH, a cluster kubeconfig, a working
StorageClass when persistence is enabled, and the auth Secret named in
the values below.
"""

import os

from multistack import Cache
from multistack.cache import CacheBackend, ValkeyOptions

# Not /tmp: that does not survive a reboot, and losing the kubeconfig
# leaves you with a cluster you cannot reach and no obvious reason why.
KUBECONFIG = os.path.expanduser(
    os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig")
)



# Name and namespace, overridable so this script can drive a real
# deployment as well as the demo it defaults to. The defaults stay
# `valkey-test` deliberately: an example that defaults to a production
# release name is one `python3` away from reconfiguring it.
NAME = os.environ.get("MULTISTACK_VALKEY_NAME", "valkey-test")
NAMESPACE = os.environ.get("MULTISTACK_VALKEY_NS", "valkey-test")

# The Secret the chart reads the password from, in NAMESPACE.
AUTH_SECRET = os.environ.get("MULTISTACK_VALKEY_SECRET", "valkey-auth")

cache = Cache(
    # Required — no ambient $KUBECONFIG fallback, so this can only ever
    # act on the cluster you explicitly specify.
    kubeconfig_path=KUBECONFIG,

    # Helm release identity.
    name=NAME,
    namespace=NAMESPACE,

    # `valkey` is the only implementation today, and the default, so
    # naming it is optional — spelled out because an example that hides
    # the choice teaches that there isn't one.
    type="valkey",

    # Chart and values are Bitnami-chart specifics, so they live on the
    # implementation's options rather than on the spec: a second cache
    # implementation would not have this chart or these values.
    options=ValkeyOptions(
        # The common Helm layer handles Bitnami repository resolution.
        chart="valkey",

        # Values passed to the Valkey Helm chart.
        values={
            "architecture": "standalone",

            # The password is not in this spec. It lives in a Kubernetes
            # Secret that the chart reads directly, created before the
            # release, in the release's namespace:
            #
            #   import base64, secrets
            #   from multistack.kube import apply
            #   apply(KUBECONFIG, {"apiVersion": "v1", "kind": "Namespace",
            #                      "metadata": {"name": "valkey-test"}})
            #   apply(KUBECONFIG, {
            #       "apiVersion": "v1", "kind": "Secret",
            #       "metadata": {"name": "valkey-auth",
            #                    "namespace": "valkey-test"},
            #       "type": "Opaque",
            #       "data": {"valkey-password": base64.b64encode(
            #           secrets.token_urlsafe(24).encode()).decode()},
            #   })
            #
            # Not `kubectl create secret --from-literal`: that puts the
            # password in the process arguments, where any local user can read
            # it out of /proc, and in the shell history besides. `apply` pipes
            # the manifest to stdin, so it reaches neither.
            #
            # A spec gets printed, logged, diffed and committed; a password
            # written here ends up in all four. Passing it through the chart
            # keeps it in etcd and in the pod, which is where it belongs.
            "auth": {
                "enabled": True,
                "existingSecret": AUTH_SECRET,
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

release = CacheBackend().create(cache)

print(f"\nValkey release {release.name} is {release.status.value}")
print(f"  namespace: {release.namespace}")
print(f"  revision:  {release.revision}")
print(f"  chart:     {release.chart_name}")
print(f"  version:   {release.chart_version}")