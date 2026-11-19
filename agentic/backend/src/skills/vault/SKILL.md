---
name: vault
description: Use this skill when the user's request mentions Vault, HashiCorp Vault, secrets management, storing secrets, unsealing, a Raft cluster for secrets, or where generated credentials should be kept. Covers the real MultiStack SDK Vault/VaultOptions/VaultBackend classes.
---

# MultiStack SDK — Vault / VaultOptions / VaultBackend

Secrets management: one Vault, holding the credentials every other
capability generates and would otherwise print to a terminal or leave in
a `.env` file. `type="vault"` is the only implementation. Like
Storage/SeaweedFS/Gateway/Policy/Inference, Vault deploys onto an
**existing** cluster — it needs a real `kubeconfig_path`.

`Vault`/`VaultOptions` are Pydantic models, and `build_vault_plan`'s
`vault` parameter is typed as `Vault` directly — the field-by-field shape
below is generated straight from these classes for the tool schema a
model actually receives, not retyped by hand.

```python
class VaultOptions(BaseModel):
    release_name: str = "vault"
    chart: str = "vault"
    cpu_request: str = "250m"
    cpu_limit: str = "500m"
    memory_request: str = "256Mi"
    memory_limit: str = "512Mi"
    data_size: str = "10Gi"
    storage_class: Optional[str] = None   # None = the cluster's default StorageClass
    ha_enabled: bool = True               # Raft (integrated storage), 3 pods -- NOT opt-in, see rules
    replicas: int = 3                     # Raft members; only meaningful when ha_enabled=True -- see rules
    ui_enabled: bool = True
    injector_enabled: bool = False        # the Agent Injector sidecars Vault into OTHER pods -- nothing here uses it yet
    extra_values: Dict[str, Any] = {}

    @property
    def effective_replicas(self) -> int: ...   # replicas if ha_enabled else 1 -- the real pod count

class Vault(CapabilitySpec):
    type: str = "vault"
    kubeconfig_path: str              # required -- the EXISTING cluster this installs onto
    namespace: Optional[str] = None   # defaults to "vault"
    chart_version: Optional[str] = None
    storage_class: Optional[str] = None
    extra_values: Dict[str, Any] = {}
    options: Optional[VaultOptions] = None

    # Exposed through the Gateway API, like every external-facing
    # capability here -- never a raw Ingress (see the `route` skill).
    hostnames: List[str] = []         # empty = match on path alone, the bare-LAN-address default
    route_path_prefix: str = "/"      # must start with "/"
    parent_name: str = "platform-gw"
    parent_namespace: str = "istio-ingress"
    ingress_service: str = "istio-ingressgateway"

    @property
    def internal_addr(self) -> str: ...   # in-cluster URL, no token in it -- PROVIDES vault_addr
    @property
    def ui_url(self) -> str: ...          # external UI URL once the HTTPRoute is applied

class VaultCreateResult(BaseModel):
    ui_url: str
    admin_username: str
    newly_initialized: bool
    init_file: str
    # These three are populated ONLY when newly_initialized is True --
    # None on every later, idempotent call. See rules below.
    admin_password: Optional[str] = None
    root_token: Optional[str] = None
    unseal_keys: Optional[List[str]] = None

class VaultBackend:
    def create(self, vault: Vault) -> VaultCreateResult: ...
    def delete(self, vault: Vault, delete_data: bool = False) -> None: ...
    def check_prerequisites(self, vault: Vault) -> list: ...
    def store_secret(self, vault: Vault, path: str, data: dict) -> None: ...
    def get_secret(self, vault: Vault, path: str) -> dict: ...
```

Rules when generating code:
- `kubeconfig_path` is required and has no ambient fallback — the SDK deliberately never reads `$KUBECONFIG`/`~/.kube/config`, so a stale default can't silently target the wrong cluster.
- `build_vault_plan` is for adding Vault to a cluster that **already exists**. If the cluster is also being created in this same request, use `build_full_stack_plan` instead.
- Vault depends on a cluster **and block storage** (`REQUIRES = ("cluster", "storage")`) — Vault's own data sits on a PVC, exactly like a MinIO tenant. Don't propose it before a StorageClass exists (see the `storage` skill), unless the cluster already has a default one.
- **HA (3-replica Raft) is the default, not opt-in.** Vault is a single point of failure for every credential this platform holds once another module actually calls `store_secret`, so a single pod that a node drain takes down is treated as unacceptable by default. Leave `ha_enabled`/`replicas` at their defaults unless the user explicitly asks for a throwaway/lab Vault.
- `ha_enabled=True` requires an **odd** `replicas` count (Raft needs a real majority) — the SDK rejects an even number. `ha_enabled=False` only makes sense with `replicas=1`; writing any other explicit value alongside `ha_enabled=False` is rejected outright, since the chart would still only ever produce one pod in that mode. Use `effective_replicas` (not `replicas`) when you need the actual pod count — they disagree whenever `ha_enabled=False`.
- **SECURITY — never invent, guess, or print a value for `admin_password`, `root_token` or `unseal_keys`.** All three are generated by Vault itself (`vault operator init`), not supplied by the user — unlike a CNPG `database.password`, there is nothing to ask the user for here. They come back on `VaultCreateResult` **only** on the one call that actually initializes Vault (`newly_initialized=True`); every later, idempotent call returns `None` for all three, because by then the on-disk init file is the only copy that matters. Never claim a value is available outside that one response.
- `storage_class` is set directly on the spec (not inside `options`) — `None` means "the cluster's default StorageClass", the same convention `Storage.default_storage_class` uses.
- `hostnames`/`route_path_prefix`/`parent_name`/`parent_namespace`/`ingress_service` expose the Vault UI through the Gateway API, the same shape `build_route_plan` uses — never propose a raw `Ingress` object for it. Leave these at their defaults unless the user gives a real hostname or a non-default ingress to bind to.
- Once deployed, this Vault's `internal_addr` (`PROVIDES: vault_addr`) is the in-cluster URL another capability would use to reach it — it carries no token. There is no other capability today that reads `vault_addr` automatically; wiring a module to actually store secrets here is still a manual step outside what this tool renders.
