"""Deploy Valkey as the environment's Redis-compatible cache."""

import os

from multistack import Cache
from multistack.cache import CacheBackend, ValkeyOptions

KUBECONFIG = os.path.expanduser(os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig"))

ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev").strip().lower()
if ENVIRONMENT == "main":
    ENVIRONMENT = "prod"
elif ENVIRONMENT in {"e2e", "e2e-test"}:
    ENVIRONMENT = "e2e-test"
elif ENVIRONMENT not in {"dev", "qa", "staging", "prod"}:
    raise ValueError("ENVIRONMENT must be dev, qa, staging, prod, or e2e-test")

NAMESPACE = os.environ.get("NAMESPACE") or f"{ENVIRONMENT}-redis"
RELEASE_NAME = os.environ.get("VALKEY_NAME") or "valkey"
STORAGE_CLASS = os.environ.get("STORAGE_CLASS") or "longhorn"
STORAGE_SIZE = os.environ.get("STORAGE_SIZE") or "10Gi"
REPLICA_COUNT = int(os.environ.get("VALKEY_REPLICA_COUNT", "1"))
PERSISTENCE_ENABLED = os.environ.get("VALKEY_PERSISTENCE_ENABLED", "false").strip().lower()
if PERSISTENCE_ENABLED not in {"true", "false"}:
    raise ValueError("VALKEY_PERSISTENCE_ENABLED must be true or false")
PERSISTENCE_ENABLED = PERSISTENCE_ENABLED == "true"

cache = Cache(
    kubeconfig_path=KUBECONFIG,
    name=RELEASE_NAME,
    namespace=NAMESPACE,
    type="valkey",
    options=ValkeyOptions(
        chart="oci://registry-1.docker.io/bitnamicharts/valkey",
        chart_version=os.environ.get("VALKEY_CHART_VERSION") or "6.3.13",
        values={
            "architecture": "replication",
            "auth": {"enabled": False},
            "sentinel": {"enabled": True},
            "primary": {
                "persistence": {
                    "enabled": PERSISTENCE_ENABLED,
                    "storageClass": STORAGE_CLASS,
                    "size": STORAGE_SIZE,
                },
            },
            "replica": {
                "replicaCount": REPLICA_COUNT,
                "persistence": {
                    "enabled": PERSISTENCE_ENABLED,
                    "storageClass": STORAGE_CLASS,
                    "size": STORAGE_SIZE,
                },
            },
        },
    ),
)

release = CacheBackend().create(cache)

print(f"\nRedis-compatible Valkey release {release.name} is {release.status.value}")
print(f"  namespace: {release.namespace}")
print(f"  endpoint:  {cache.endpoint}")
print(f"  VALKEY_URL: {cache.endpoint}")
