---
name: storage
description: Use this skill when the user's request mentions storage, a StorageClass, persistent volumes, Longhorn, or block storage. Covers the real MultiStack SDK Storage/LonghornOptions/StorageBackend classes.
---

# MultiStack SDK — Storage / LonghornOptions / StorageBackend

Real class signatures (from `multistack.storage`, re-exported at the top
level as `from multistack import Storage, StorageBackend`). Block
storage installs *into an existing cluster* — it does not create one.
"longhorn" is the only supported implementation today.

`Storage`/`LonghornOptions` are Pydantic models, and `build_storage_plan`'s
`storage` parameter is typed as `Storage` directly — the field-by-field
shape below is generated straight from these classes for the tool schema
a model actually receives, not retyped by hand. What's below is for a
human reader; it can't drift out of sync with the tool's own schema the
way a hand-written description could.

```python
class LonghornOptions(BaseModel):
    release_name: str = "longhorn"
    data_path: str = "/var/lib/longhorn"       # must be an absolute path
    kubelet_root_dir: str = "/var/lib/kubelet"  # must be an absolute path
    enable_rwx: bool = True                       # needs an NFSv4 client on every node if True
    helm_repo_name: str = "longhorn"
    helm_repo_url: str = "https://charts.longhorn.io"

class Storage(BaseModel):
    type: str                       # required -- only "longhorn" today, no default (an explicit choice)
    kubeconfig_path: str            # required -- the EXISTING cluster this installs onto
    replica_count: int = 3          # copies of each volume; needs at least this many schedulable nodes
    default_storage_class: bool = True
    namespace: Optional[str] = None    # None means the implementation's own default
    chart_version: Optional[str] = None
    extra_values: dict = {}
    options: Optional[LonghornOptions] = None   # auto-filled from `type` if left unset

    def validate(self) -> None: ...   # raises ValueError on:
        # - type missing, or not one of the supported types
        # - kubeconfig_path empty
        # - replica_count < 1
        # - options belonging to a different implementation than `type`

class StorageBackend:
    def install_prerequisites(self, storage: Storage, nodes) -> None: ...  # real per-node checks over SSH
    def create(self, storage: Storage, nodes=None) -> str: ...              # returns the StorageClass name
    def delete(self, storage: Storage) -> None: ...
```

Rules when generating code:
- `type` is always explicit — never default it or guess; ask if the user didn't say which implementation (today, "longhorn" is the only real answer, but the field stays required on purpose).
- `kubeconfig_path` is required and has no ambient fallback — the SDK deliberately never reads `$KUBECONFIG`/`~/.kube/config`, so a stale default can't silently target the wrong cluster.
- `build_storage_plan` is for adding storage to a cluster that **already exists** — it needs a real `kubeconfig_path` and the cluster's real node list. If the cluster is also being created in this same request, use `build_full_stack_plan` instead, which wires the two together without needing to already know the new cluster's kubeconfig.
- Storage depends on a cluster (`REQUIRES = ("cluster",)`) — don't propose installing storage before a cluster exists.
- Leave `options` unset unless the user describes a specific need (a non-default data path, disabling RWX support, pinning a chart version) — it auto-fills from `type` with working defaults otherwise.
- The StorageClass this produces is named after `options.release_name` (or `type` if that's unset) — this is the value a MinIO tenant or a `VolumeClaim` should reference as `storage_class`.

See `docs/storage.md` in this repo for the full human-facing reference — this file is the condensed version scoped to what a model needs to generate a correct tool call, not the complete picture.
