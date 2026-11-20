"""
Deploy a SeaweedFS cluster with the Multistack SDK.

S3-compatible object storage on an existing cluster, via the official
SeaweedFS Helm chart — one release runs master, volume, filer and the S3
gateway together. This is the right tier for large immutable blobs —
model weights, datasets, backups — and `create()` returns the endpoint a
consumer needs.

    pip install -e .                          # from the repo root
    python3 examples/seaweedfs/cluster.py

Needs `helm` and `kubectl` on PATH, a cluster kubeconfig, and a working
StorageClass (a fresh RKE2 cluster has none — see examples/storage/install.py).
"""
import os

from multistack import SeaweedFSCluster
from multistack.backends.seaweedfs_client import SeaweedFSBackend

# Not /tmp: that does not survive a reboot, and losing the kubeconfig
# leaves you with a cluster you cannot reach and no obvious reason why.
KUBECONFIG = os.path.expanduser(
    os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig")
)

cluster = SeaweedFSCluster(
    # Required — no ambient $KUBECONFIG fallback, so this can only ever
    # act on the cluster you name.
    kubeconfig_path=KUBECONFIG,
    name="seaweedfs-test",
    namespace="seaweedfs-test",

    # SeaweedFS has no MinIO-style "4 drives minimum" erasure-coding
    # rule — durability comes from `replication` below, not from how many
    # replicas of each component exist.
    master_replicas=1,
    filer_replicas=1,
    volume_replicas=2,
    volume_size="30Gi",

    # One extra copy on another server in the same rack. "000" (the
    # chart's own default) keeps no redundancy at all — fine for a lab,
    # not for anything you can't re-create.
    replication="001",

    # Name it explicitly. Left unset the cluster default is used, which
    # is fine until someone changes which class is default.
    storage_class="longhorn",

    # Leave access_key/secret_key unset and they're generated, so the
    # cluster is never created with a credential everyone can guess. They
    # come back on the result below — store them somewhere real.
)

info = SeaweedFSBackend().create(cluster)

print(f"\nCluster {info.name} is {info.status}")
print(f"  endpoint:    {info.endpoint}")
print(f"  access key:  {info.access_key}")
print(f"  secret key:  {info.secret_key}")
print(f"  pods:        {', '.join(info.pod_names)}")
print("\nTo serve model weights from here, hand the endpoint to Inference:")
print(f"""  Inference(
      model="s3://models/<name>",
      s3_endpoint_url="{info.endpoint}",
      s3_secret_name="seaweedfs-creds",
  )""")
