---
name: seaweedfs
description: Use this skill when the user's request mentions SeaweedFS, MinIO, object storage, S3, S3-compatible storage, buckets, or somewhere to keep model weights or datasets. Covers the real MultiStack SDK SeaweedFSCluster/SeaweedFSBackend classes.
---

# MultiStack SDK — SeaweedFSCluster / SeaweedFSBackend

Real class signature (from `multistack.core.seaweedfs`, re-exported at the
top level as `from multistack import SeaweedFSCluster`). **This is the
platform's object storage.** It replaced MinIO, whose images can no longer
be pulled without an authenticated account. One Helm release runs the
master, volume servers, filer and S3 gateway together, installed *into an
existing cluster that already has block storage* — its volumes bind
against a real StorageClass.

`SeaweedFSCluster` is a Pydantic model, and `build_seaweedfs_plan`'s
`cluster` parameter is typed as `SeaweedFSCluster` directly — the tool
schema the model receives is generated from the class itself.

```python
class SeaweedFSCluster(BaseModel):
    kubeconfig_path: str             # required -- the EXISTING cluster this installs onto
    name: str                        # required

    namespace: str = "seaweedfs"

    master_replicas: int = 1         # each >= 1
    filer_replicas: int = 1
    volume_replicas: int = 1

    volume_size: str = "10Gi"        # a Kubernetes quantity -- must include a unit
    metadata_size: str = "8Gi"       # the master's own volume, for its metadata store -- same unit rule
    storage_class: Optional[str] = None   # None means "use the cluster's default StorageClass"

    # Durability: SeaweedFS's replication code "XYZ" -- extra copies in
    # other data centers (X), other racks (Y), other servers in the same
    # rack (Z). "000" (the default) keeps no extra copy; "001" keeps one
    # on another server. Three digits, nothing else.
    replication: str = "000"

    access_key: Optional[str] = None   # leave unset -- generated automatically if omitted
    secret_key: Optional[str] = None   # leave unset -- generated automatically if omitted

    # Chart/release plumbing -- leave alone unless the user names a need.
    release_name: Optional[str] = None   # None = use `name`
    chart_name: str = "seaweedfs"
    helm_repo_name: str = "seaweedfs"
    helm_repo_url: str = "https://seaweedfs.github.io/seaweedfs/helm"
    s3_port: int = 8333                # the chart's S3 gateway port
    image: Optional[str] = None
    extra_values: Dict[str, Any] = {}  # merged into the Helm values

    # matchLabels selectors allowed to reach the S3 API. Empty => NO
    # NetworkPolicy is installed at all.
    network_policy_allowed_ingress: List[Dict[str, str]] = []

    def endpoint(self) -> str: ...     # http://<release>-seaweedfs-s3.<namespace>.svc.cluster.local:8333

class SeaweedFSBackend:
    def create(self, cluster: SeaweedFSCluster, wait_for_ready=True) -> SeaweedFSClusterInfo: ...
    def delete(self, cluster: SeaweedFSCluster, delete_pvcs=False) -> None: ...
    def endpoint(self, cluster: SeaweedFSCluster) -> str: ...   # in-cluster S3 URL
```

Rules when generating code:
- `kubeconfig_path` is required and has no ambient fallback — same reasoning as every other capability in this SDK.
- **SeaweedFS is the only object storage this agent plans.** If the user asks for MinIO, explain that the platform replaced it with SeaweedFS (MinIO's images can no longer be pulled without an authenticated account) and offer SeaweedFS instead.
- `build_seaweedfs_plan` is for adding SeaweedFS to a cluster **that already has block storage installed** — it needs a real `kubeconfig_path` and, usually, a real `storage_class` name. If the cluster or storage is also being created in this request, use `build_full_stack_plan` instead, which wires the layers together.
- SeaweedFS depends on a cluster and block storage (`REQUIRES = ("cluster", "storage")`) — don't propose it before both exist.
- Never invent `access_key`/`secret_key` — leave both unset (the SDK generates real credentials) unless the user gives their own values for **both**; setting one without the other is rejected.
- `replication` is how many extra copies of each object are kept, as three digits. There is no minimum drive count; for any redundancy the user asks for, pick the code (e.g. `"001"` for one extra copy on another server) and give `volume_replicas` enough servers to hold the copies.
- `volume_size` and `metadata_size` must include a unit (`"10Gi"`, not `"10"`) — a bare number is bytes. Leave `metadata_size` at its default unless the user asks for a specific size for the master's own metadata store, a separate thing from the volume servers' `volume_size`.
- To serve model weights from it, point `Inference.s3_endpoint_url` at its `endpoint()` (with an `s3://` model). `build_full_stack_plan` wires that automatically when both are composed. The S3 API is plain HTTP; TLS is not modelled.
- `network_policy_allowed_ingress` is **off by default**: an empty list installs no NetworkPolicy. Only set it when the user asks to restrict access, with real selector labels they gave you — anything reading from it, an Inference deployment for one, must be named in it, or the reads silently fail.
- The `release_name`/`chart_name`/`helm_repo_*`/`image`/`extra_values` fields have working defaults — leave them alone unless the user names a specific need, such as an internal Helm mirror.
