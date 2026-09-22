"""
Deploy a MinIO tenant with the Multistack SDK.

S3-compatible object storage on an existing cluster, via the MinIO
Operator Helm charts. This is the right tier for large immutable blobs —
model weights, datasets, backups — and `create()` returns the endpoint a
consumer needs.

    pip install -e .                          # from the repo root
    python3 examples/minio/tenant.py

Needs `helm` and `kubectl` on PATH, a cluster kubeconfig, and a working
StorageClass (a fresh RKE2 cluster has none — see examples/storage/install.py).
"""
import os

from multistack import MinIOTenant
from multistack.backends.minio_client import MinIOBackend

# Not /tmp: that does not survive a reboot, and losing the kubeconfig
# leaves you with a cluster you cannot reach and no obvious reason why.
KUBECONFIG = os.path.expanduser(
    os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig")
)

tenant = MinIOTenant(
    # Required — no ambient $KUBECONFIG fallback, so this can only ever
    # act on the cluster you name.
    kubeconfig_path=KUBECONFIG,
    name="minio-test",
    namespace="minio-test",

    # 2 servers x 2 volumes = 4 drives, the minimum for erasure coding.
    # The spec rejects a distributed tenant with fewer up front, rather
    # than letting the operator fail after it's been installed.
    mode="distributed",
    servers=2,
    volumes_per_server=2,
    volume_size="30Gi",

    # Name it explicitly. Left unset the cluster default is used, which
    # is fine until someone changes which class is default.
    storage_class="longhorn",

    # Leave root_user/root_password unset and they're generated, so the
    # tenant is never created with a credential everyone can guess. They
    # come back on the result below — store them somewhere real.
)

info = MinIOBackend().create(tenant)

print(f"\nTenant {info.name} is {info.status}")
print(f"  endpoint: {info.endpoint}")
print(f"  user:     {info.root_user}")
print(f"  password: {info.root_password}")
print(f"  pods:     {', '.join(info.pod_names)}")
print("\nTo serve model weights from here, hand the endpoint to Inference:")
print(f"""  Inference(
      model="s3://models/<name>",
      s3_endpoint_url="{info.endpoint}",
      s3_secret_name="minio-creds",
      s3_insecure_tls={tenant.request_auto_cert},   # operator's own CA
  )""")
