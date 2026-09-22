---
name: valkey
description: Use this skill when the user's request mentions Valkey, a Redis-compatible cache, or a counter/cache store to deploy. Covers the real MultiStack SDK Cache/CacheBackend classes.
---

# MultiStack SDK — Cache / CacheBackend (type="valkey")

Real class signature (from `multistack`, re-exported at the top level
as `from multistack import Cache`, with `CacheBackend` and
`ValkeyOptions` at `multistack.cache`). The spec is `Cache`, not
`Valkey`: it names the capability, and `type` names the implementation,
the same way `Storage(type="longhorn")` does. `valkey` is the only
`type` today (the Bitnami Valkey Helm chart) and is the default.

A cache installs *into an existing cluster* — it does not create one.

`Cache` is a Pydantic model, and `build_valkey_plan`'s `valkey`
parameter is typed as `Cache` directly — the field-by-field shape below
is generated straight from the class for the tool schema a model
actually receives, not retyped by hand.

```python
class ValkeyOptions(BaseModel):
    chart: str = "valkey"
    chart_version: Optional[str] = None
    values: dict = {}           # raw Helm chart values, for anything this spec doesn't model directly

class Cache(CapabilitySpec):
    type: str = "valkey"        # the only implementation today
    kubeconfig_path: str        # required -- the EXISTING cluster this installs onto
    name: str                   # required -- identifies the Helm release
    namespace: Optional[str] = None    # None => the implementation's default, "valkey"
    options: Optional[ValkeyOptions] = None   # filled in automatically to match `type`

    def validate(self) -> None: ...   # raises ValueError on:
        # - kubeconfig_path empty
        # - name empty
        # - an unknown type, or options belonging to another one

    @property
    def endpoint(self) -> str: ...   # real, computed redis://... URL -- see rules below

    @property
    def resolved_namespace(self) -> str: ...   # the namespace given, else the type's default

class CacheBackend:
    def create(self, cache: Cache) -> HelmRelease: ...
    def update(self, cache: Cache, values=None) -> HelmRelease: ...
    def delete(self, cache: Cache) -> None: ...
    def check_prerequisites(self, cache: Cache) -> None: ...
```

Rules when generating code:
- `kubeconfig_path` is required and has no ambient fallback — the SDK deliberately never reads `$KUBECONFIG`/`~/.kube/config`, so a stale default can't silently target the wrong cluster.
- A cache depends on a cluster (`REQUIRES = ("cluster",)`) — don't propose installing it before a cluster exists.
- `Cache.endpoint` IS a real, computed `redis://...` connection string — `build_valkey_plan` returns it as `endpoint` in its result. It depends on more than just `name`: Bitnami's chart collapses the release name into the Service name differently depending on `values` (`nameOverride`/`fullnameOverride`, Sentinel on/off) — always use the `endpoint` this tool actually returns, never construct one yourself from `name`/`namespace` by hand.
- Once deployed, this Valkey's `endpoint` is what goes into a Policy's `cache_url` (see the `policy` skill / `build_policy_plan`) to actually use it as the rate-limiter's counter store — deploying Valkey alone does nothing on its own.
- `chart`, `chart_version` and `values` live on `options`, not on the spec itself — they are Bitnami-chart specifics, and a second cache implementation would not have them. Pass them as `options={"values": {...}}`, never at the top level: the spec forbids unknown fields and will reject it.
- `values` is for raw Helm chart settings this spec doesn't model directly — leave it empty (`{}`) unless the user names a specific chart setting they want changed.
- This tool IS composable into `build_full_stack_plan` (as `valkey`), and unlike Gateway/Policy (which never auto-link to each other), composing `valkey` alongside `policy` in the same call DOES wire the real cache endpoint into `Policy.cache_url` automatically — see `mcp_tools/full_stack.py`'s own docstring for exactly how.
