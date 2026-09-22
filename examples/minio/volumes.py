"""
Inspect and grow a MinIO tenant's volumes.

A tenant's PVCs bind against the StorageClass the `storage` capability
installs (see examples/storage/install.py) — object storage on top of
block storage, which is why `storage_class` below names it.

Read-only by default. The destructive calls are commented out on
purpose: this file names a real tenant, and deleting its PVCs destroys
the data irrecoverably. Uncomment only the one you mean.

    python3 examples/minio/volumes.py
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
    kubeconfig_path=KUBECONFIG,
    name="minio-test",
    namespace="minio-test",
    servers=2,
    volumes_per_server=2,
    volume_size="30Gi",
    storage_class="longhorn",
)
backend = MinIOBackend()

print("PVCs:", backend.pvc_sizes(tenant))

# --- Grow the volumes in place ---------------------------------------------
# Needs the StorageClass to have allowVolumeExpansion: true. Shrinking is
# refused before any API call, since Kubernetes cannot shrink a PVC.
# print("resized:", backend.resize_storage(tenant, "50Gi"))

# --- Remove the release, keep the data --------------------------------------
# backend.delete(tenant)

# --- Remove the release AND the data ----------------------------------------
# Irreversible.
# backend.delete(tenant, delete_pvcs=True)
