---
name: minio
description: Use this skill when the user's request mentions MinIO, object storage, S3, buckets, or a tenant for storing model weights/datasets. Covers the real MultiStack SDK MinIOTenant/MinIOBackend classes.
---

# MultiStack SDK — MinIOTenant / MinIOBackend

Real class signature (from `multistack.core.minio`, re-exported at the
top level as `from multistack import MinIOTenant`). A tenant installs
S3-compatible object storage *into an existing cluster that already has
block storage* — it needs a real StorageClass to bind its volumes
against.

`MinIOTenant` is a Pydantic model, and `build_minio_plan`'s `tenant`
parameter is typed as `MinIOTenant` directly — the field-by-field shape
below is generated straight from the class for the tool schema a model
actually receives, not retyped by hand. What's below is for a human
reader; it can't drift out of sync with the tool's own schema the way a
hand-written description could.

```python
class MinIOTenant(BaseModel):
    kubeconfig_path: str             # required -- the EXISTING cluster this installs onto
    name: str                        # required

    namespace: str = "minio"
    mode: str = "distributed"         # "standalone" or "distributed"

    servers: int = 4
    volumes_per_server: int = 2
    volume_size: str = "10Gi"         # a Kubernetes quantity -- must include a unit
    storage_class: Optional[str] = None   # None means "use the cluster's default StorageClass"

    # Chart/release plumbing -- leave every one of these alone unless the
    # user names a specific need (an internal mirror, a renamed release).
    operator_release_name: str = "minio-operator"
    operator_namespace: str = "minio-operator"
    operator_chart: str = "operator"
    tenant_chart: str = "tenant"
    helm_repo_name: str = "minio"
    helm_repo_url: str = "https://operator.min.io/"

    # matchLabels selectors allowed to reach this tenant. Empty => NO
    # NetworkPolicy is installed at all, so an existing deployment is
    # untouched until a caller opts in.
    network_policy_allowed_ingress: List[Dict[str, str]] = []

    root_user: Optional[str] = None       # leave unset -- generated automatically if omitted
    root_password: Optional[str] = None   # leave unset -- generated automatically if omitted

    request_auto_cert: bool = True
    image: Optional[str] = None
    extra_values: dict = {}

    def validate(self) -> None: ...   # raises ValueError on:
        # - kubeconfig_path or name/namespace empty
        # - mode not "standalone" or "distributed"
        # - servers or volumes_per_server < 1
        # - mode="standalone" with servers != 1
        # - mode="distributed" with servers * volumes_per_server < 4 (erasure coding needs 4+ drives)
        # - volume_size not a valid Kubernetes quantity (e.g. "10" is bytes, not GB -- always include a unit)
        # - root_user set without root_password, or vice versa

class MinIOBackend:
    def create(self, tenant: MinIOTenant, wait_for_ready=True, install_operator=True) -> MinIOTenantInfo: ...
    def delete(self, tenant: MinIOTenant, delete_pvcs=False) -> None: ...
    def endpoint(self, tenant: MinIOTenant) -> str: ...   # in-cluster S3 URL
```

Rules when generating code:
- `kubeconfig_path` is required and has no ambient fallback — same reasoning as every other capability in this SDK (see the storage/rke2-cluster skills).
- `build_minio_plan` is for adding MinIO to a cluster **that already has block storage installed** — it needs a real `kubeconfig_path` and, usually, a real `storage_class` name. If the cluster or storage is also being created in this same request, use `build_full_stack_plan` instead, which wires the layers together without needing values only a running cluster/storage layer can produce.
- Never invent a value for `root_user`/`root_password` — leave both unset (the SDK generates real credentials) unless the user explicitly gives their own values for both. This is the same "don't guess a value meant to be generated" rule as `RKE2Cluster.token`.
- `mode="distributed"` needs `servers * volumes_per_server >= 4` for erasure coding — don't propose a smaller topology unless the user explicitly asked for `mode="standalone"` (which requires exactly one server).
- `volume_size` must include a unit (`"10Gi"`, not `"10"`) — a bare number is bytes, not the size the user meant.
- MinIO depends on both a cluster and block storage (`REQUIRES = ("cluster", "storage")`) — don't propose a tenant before both exist.
- `network_policy_allowed_ingress` locks down who may reach this tenant, and it is **off by default**: an empty list installs no NetworkPolicy at all. Only set it when the user asks to restrict ingress, with real selector labels they gave you — note that anything reading model weights from this tenant (an Inference deployment, for one) must be named in it, or the pull silently fails.
- The `operator_*`/`tenant_chart`/`helm_repo_*` fields are chart plumbing with working defaults — leave them alone unless the user names a specific need, such as pulling from an internal Helm mirror instead of `https://operator.min.io/`.

See `docs/minio.md` in this repo for the full human-facing reference — this file is the condensed version scoped to what a model needs to generate a correct tool call, not the complete picture.
