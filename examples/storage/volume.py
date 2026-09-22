"""
Claim a volume from the storage the SDK installed, grow it, remove it.

    python3 examples/storage/volume.py

Needs a cluster with block storage already installed — see
examples/storage/install.py.

DESTRUCTIVE at the end: delete_claim() destroys the volume's data, because
Longhorn's StorageClass uses the default Delete reclaim policy. The call is
left uncommented here only because this claim is created a few lines above
and holds nothing.
"""
import os

from multistack import Storage, StorageBackend, VolumeClaim

# Not /tmp: that does not survive a reboot, and losing the kubeconfig
# leaves you with a cluster you cannot reach and no obvious reason why.
KUBECONFIG = os.path.expanduser(
    os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig")
)

storage = Storage(
    type="longhorn",
    kubeconfig_path=KUBECONFIG,
)

claim = VolumeClaim(
    name="example-data",
    namespace="default",
    # A Kubernetes quantity, and the unit is required: a bare "10" is ten
    # *bytes*, which binds and is instantly full.
    size="1Gi",
    # ReadWriteOnce is one node at a time. ReadWriteMany needs an
    # implementation that serves it — for Longhorn that means
    # enable_rwx=True and an NFSv4 client on every node.
    access_mode="ReadWriteOnce",
    # Left unset, the claim uses whichever StorageClass the Storage spec
    # produces. Hardcoding a name here is how a claim ends up Pending
    # against a class that isn't there.
)

backend = StorageBackend()

# Waits for Bound by default. Worth it: an unbound PVC isn't an error, it
# sits Pending, and whatever tries to mount it fails instead — which is
# where the blame lands.
storage_class = backend.create_claim(storage, claim)
print(f"bound against StorageClass '{storage_class}'")

# Only grows. Kubernetes cannot shrink a PVC, and the class needs
# allowVolumeExpansion — both are checked before the patch, rather than
# leaving you with a patch that applied and did nothing.
backend.resize_claim(storage, claim, "2Gi")

backend.delete_claim(storage, claim)
