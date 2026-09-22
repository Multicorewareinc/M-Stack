---
name: cnpg
description: Use this skill when the user's request mentions Postgres, PostgreSQL, CloudNativePG, CNPG, or a database to deploy. Covers the real MultiStack SDK Database/DatabaseConfig/CNPGOptions/DatabaseBackend classes.
---

# MultiStack SDK — Database / DatabaseConfig / DatabaseBackend (type="cnpg")

Real class signatures (from `multistack`, re-exported at the top level
as `from multistack import Database, DatabaseConfig`, with
`DatabaseBackend` and `CNPGOptions` at `multistack.database`). The spec
is `Database`, not `CNPG`: it names the capability, and `type` names the
implementation, the same way `Storage(type="longhorn")` and
`Cache(type="valkey")` do. `cnpg` (CloudNativePG) is the only `type`
today and is the default.

A database installs *into an existing cluster* — it does not create one.

**This capability has TWO independent lifecycles**, unlike everything
else covered so far: installing the CloudNativePG operator (a Helm
release), and creating an actual PostgreSQL cluster+database on top of
that operator. Whether `cnpg.database` is set decides which
`build_cnpg_plan` renders — leave it unset for an operator-only script,
set it for operator install followed by a real database.

That split is also what decides where a field lives. The operator's Helm
release and its CRD are CloudNativePG-specific, so they sit in
`CNPGOptions`; the cluster's own shape (instances, storage, scheduling)
is what anyone would ask a database for, so it sits on `Database`.

```python
class DatabaseConfig(BaseModel):
    name: str      # required -- the database's own name
    owner: str     # required -- the Postgres role that owns it
    password: str  # required -- see SECURITY note below

class CNPGOptions(BaseModel):
    operator_release_name: str = "cnpg"
    operator_namespace: str = "cnpg-system"
    operator_chart: str = "cloudnative-pg"
    operator_chart_version: Optional[str] = "0.29.0"
    values: Optional[dict] = None       # raw Helm values for the operator chart
    install_timeout: str = "15m"
    crd_name: str = "clusters.postgresql.cnpg.io"   # the CRD whose presence proves the operator is installed -- leave alone

class Database(CapabilitySpec):
    type: str = "cnpg"                  # the only implementation today
    kubeconfig_path: str                # required -- the EXISTING cluster this installs onto
    options: Optional[CNPGOptions] = None   # filled in automatically to match `type`

    # Cluster fields -- all optional so this type also works for an
    # operator-only install, but `name` and `database` travel together:
    # setting one without the other is rejected (see rules below).
    name: Optional[str] = None          # the Cluster's own name
    namespace: Optional[str] = None     # None => the implementation's default, "postgres"
    instances: int = 3
    image: str = "ghcr.io/cloudnative-pg/postgresql:17"
    storage_size: str = "10Gi"
    storage_class: Optional[str] = None
    database: Optional[DatabaseConfig] = None

    ready_timeout: int = 900
    ready_poll_interval: int = 15
    affinity: Optional[dict] = <keeps Postgres off the control-plane nodes by default>
    node_selector: Optional[dict] = None
    tolerations: Optional[list] = None

    @property
    def endpoint(self) -> Optional[str]: ...   # real postgresql://... URL, no credential in it; None until name+database are both set

    @property
    def resolved_namespace(self) -> str: ...   # the namespace given, else the type's default
```

Rules when generating code:
- `kubeconfig_path` is required and has no ambient fallback — the SDK deliberately never reads `$KUBECONFIG`/`~/.kube/config`, so a stale default can't silently target the wrong cluster.
- A database depends on a cluster only — don't propose installing it before a cluster exists.
- **SECURITY — `database.password` is a REAL, PLAINTEXT value.** There is currently no way to reference an existing Kubernetes Secret instead — this is a known, team-approved interim limitation (other components have the same gap; a future SDK change is expected to migrate this to Secret-based handling), not something this tool is missing. Because of this: **never invent, guess, or suggest a password yourself.** If the user wants a database created and hasn't given a real password, ask for it explicitly — treat it exactly like any other required value you can't make up, the same rule that already applies to node addresses or names.
- Setting `database` without also setting `cnpg.name` is rejected — `build_cnpg_plan` catches this itself (via the SDK's own `validate_cluster()`) and returns the real error, but don't ask the model to guess a name either; if the user wants a real database, both `name` and `database` need real values together.
- Leave `database` unset entirely for a request that's only about installing the operator (e.g. "set up CloudNativePG on my cluster") — don't invent a database the user didn't ask for.
- Leave `options` unset unless the user names a specific operator setting — it is filled in automatically to match `type`, with the defaults above.
- `namespace` unset means the implementation's own default (`postgres`); don't restate it, and read `resolved_namespace` when you need the value.
- Once deployed with a `database`, this spec's `endpoint` (returned as `database_url`) is the connection string a consumer would use — it carries no credential; the real password stays in the Secret CloudNativePG writes (`{cnpg.name}-app-secret`).
