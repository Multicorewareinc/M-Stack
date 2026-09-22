---
name: controlplane
description: Use this skill when the user's request mentions a control plane, admin API, organization API, plans/permissions administration, or org users/keys/quotas. Covers the real MultiStack SDK ControlPlane/ControlPlaneBackend classes.
---

# MultiStack SDK — ControlPlane / ControlPlaneBackend

Real class signature (from `multistack`, re-exported at the top level as
`from multistack import ControlPlane, ControlPlaneBackend`). One
capability, two instances: `type="admin"` (plans, permissions,
platform-wide administration) and `type="organization"` (one
organization's own users, keys, quotas). Both are a FastAPI service with
a Postgres schema and a migration Job, and call each other as peers over
the cluster network.

```python
# `type` selects which options class is filled in -- they differ in more
# than the name, so never hand a type one of the other's options.
class AdminControlPlaneOptions(BaseModel):
    release_name: str = "admin-control-plane"
    chart: str = "api/microservices/admin-control-plane/chart"
    chart_version: Optional[str] = None
    image_tag: Optional[str] = None
    migration_deadline_seconds: int = 120     # > 0
    migration_backoff_limit: int = 2
    # Admin-only. Idempotent per row but NOT concurrency-safe: two
    # replicas racing on a first install both insert the same plan and
    # one dies on the unique index. Leave on for a first install, off
    # when redeploying against an already-populated database.
    seed_plans: bool = True
    seed_permissions: bool = True

class OrganizationControlPlaneOptions(BaseModel):
    release_name: str = "organization-control-plane"
    chart: str = "api/microservices/organization-control-plane/chart"
    chart_version: Optional[str] = None
    image_tag: Optional[str] = None
    migration_deadline_seconds: int = 120
    migration_backoff_limit: int = 2

class ControlPlane(CapabilitySpec):
    type: str = "admin"                 # "admin" or "organization"
    kubeconfig_path: str                # required -- the EXISTING cluster
    namespace: Optional[str] = None
    options: Optional[ControlPlaneOptions] = None   # auto-filled from `type`: AdminControlPlaneOptions or OrganizationControlPlaneOptions

    existing_secret: str                # required -- see SECURITY note below
    replicas: int = 2
    service_port: int = 8000
    log_level: str = "INFO"
    peer_url: Optional[str] = None      # defaults to the peer's own Service name
    peer_timeout_ms: int = 3000
    billing_internal_url: Optional[str] = None   # billing's in-cluster URL -- see rules below
    # Scheduling. There is no in-cluster registry, so a service image
    # exists only on the node it was imported to -- a pod scheduled
    # anywhere else sits in ImagePullBackOff with nothing in the release
    # to explain it. None (the default) leaves the chart's own default in
    # place; an explicit {} OVERRIDES it with "schedule anywhere", which
    # is a different statement.
    node_selector: Optional[Dict[str, str]] = None
    tolerations: Optional[List[Dict[str, Any]]] = None

    @property
    def endpoint(self) -> str: ...      # real http://... URL, no credential
```

Rules when generating code:
- `kubeconfig_path` is required and has no ambient fallback — the SDK
  deliberately never reads `$KUBECONFIG`/`~/.kube/config`.
- **SECURITY — `existing_secret` is a Secret NAME, never a value.**
  This spec does not carry `DATABASE_URL`/`REDIS_URL`/`JWT_SECRET`/the
  API keys themselves — only the name of a Kubernetes Secret that must
  already hold them (created separately, e.g. via
  `multistack.kube.apply`, never via `kubectl create secret
  --from-literal`, which leaves the value in process arguments). Never
  invent a Secret name; if the user hasn't given one, ask.
- **A control plane is useless without its database and cache already
  running.** `REQUIRES = ("cluster", "database", "cache")` — a real
  CNPG database and a real Valkey cache must exist on the same cluster
  first (see the `cnpg`/`valkey` skills). This is NOT auto-wired: the
  SDK's `Stack`-published `database_url`/`cache_url` are deliberately
  credential-free, so they cannot serve as `DATABASE_URL`/`REDIS_URL`
  values in the Secret — that Secret has to be assembled separately,
  with real credentials, outside this SDK's scope.
- Two control planes (`admin` and `organization`) are two *instances*
  of one capability — deploying only one still validates and renders
  fine, but the peer link (`peer_url`, defaulting to the other's
  in-cluster Service name) will fail at runtime until both exist.
- `endpoint` is a real, computed in-cluster URL with no credential in
  it — quote this rather than guessing a service name.
- `billing_internal_url` is where this control plane reaches billing
  (admin: plan-change notifications; organization: the usage proxy).
  Unset is fine while billing isn't deployed, but once it is, unset
  falls back to `http://billing:8000`, which resolves nowhere on a real
  cluster. Use the Billing spec's `endpoint` — `build_full_stack_plan`
  fills it in when billing is composed. Never guess a URL.
- Leave `options` unset unless the user names a specific need — it
- `node_selector`/`tolerations` pin this service to a particular node. They matter here because there is no in-cluster registry: a service image exists only on the node it was imported to, so a pod scheduled anywhere else sits in `ImagePullBackOff` with nothing in the release to explain it. Leave both unset unless the user tells you which node holds the image — a node name or label is a real fact about their cluster, never one to invent. Note `None` and `{}` are different statements: unset leaves the chart's own default in place, while an explicit `{}` overrides it with "schedule anywhere".
  auto-fills from `type` with the right class. If they do ask for
  something, note `seed_plans`/`seed_permissions` exist only on the
  admin type, and should be turned off when redeploying against a
  database that's already populated (seeding races across replicas on
  the unique index).
