---
name: portal
description: Use this skill when the user's request mentions a portal, admin UI, organization UI, web UI, or frontend. Covers the real MultiStack SDK Portal/PortalBackend classes.
---

# MultiStack SDK — Portal / PortalBackend

Real class signature (from `multistack`, re-exported at the top level as
`from multistack import Portal, PortalBackend`). One capability, two
instances: `type="admin"` (the admin UI) and `type="organization"` (the
organization UI). Both are the same chart -- static files served by
nginx, which also reverse-proxies API requests so the browser makes
same-origin requests -- deployed twice with a different image and a
different upstream.

```python
# `type` selects which options class is filled in. They differ in the
# IMAGE, not just the name -- the shared `ui/chart` has to default to one
# of them and defaults to the organization portal, so an admin portal
# built with the wrong options silently deploys the wrong UI.
class AdminPortalOptions(BaseModel):
    release_name: str = "admin-portal"
    image_repository: str = "platformforge/admin-portal"
    chart: str = "ui/chart"
    chart_version: Optional[str] = None
    image_tag: Optional[str] = None

class OrganizationPortalOptions(BaseModel):
    release_name: str = "organization-portal"
    image_repository: str = "platformforge/organization-portal"
    chart: str = "ui/chart"
    chart_version: Optional[str] = None
    image_tag: Optional[str] = None

class Portal(CapabilitySpec):
    type: str = "admin"                 # "admin" or "organization"
    kubeconfig_path: str                # required -- the EXISTING cluster
    namespace: Optional[str] = None
    options: Optional[PortalOptions] = None   # auto-filled from `type`: AdminPortalOptions or OrganizationPortalOptions

    api_upstream: str = ""              # see rules below
    allow_no_api: bool = False
    api_prefixes: List[str] = ["/v1/", "/api/"]
    gateway_upstream: str = ""          # organization portal's chat -- see rules below
    replicas: int = 1
    service_port: int = 80
    log_level: str = "notice"
    resolver: str = "10.43.0.10"        # nginx's own DNS resolver, must be an IP
    node_selector: Optional[Dict[str, str]] = None

    @property
    def endpoint(self) -> str: ...      # real http://... URL, no credential
```

Rules when generating code:
- `kubeconfig_path` is required and has no ambient fallback — the SDK
  deliberately never reads `$KUBECONFIG`/`~/.kube/config`.
- `api_upstream` is the in-cluster URL nginx reverse-proxies API
  requests to — normally the matching ControlPlane's `endpoint` (see
  the `controlplane` skill; `type="admin"` portal pairs with
  `type="admin"` control plane, and the same for `"organization"`).
  It's required UNLESS `allow_no_api=True` is explicitly set: without a
  real upstream, every API call falls through to the single-page app
  and returns 200 with the index.html body, so the browser fails on a
  JSON parse error with nothing in any log to explain it. **Never set
  `allow_no_api=True` without the user explicitly asking** to deploy
  the UI ahead of its API.
- `gateway_upstream` is where nginx proxies `/mg/*` — the organization
  portal's chat calls the Model Gateway directly. Empty renders no `/mg`
  location, so chat gets index.html with a 200 and shows "Unable to load
  models" with no error anywhere. Set it to the Gateway's `endpoint`
  for an organization portal once a gateway exists; leave it empty for
  an admin portal, which has no chat. `build_full_stack_plan` fills it
  in when a gateway is composed.
- **This rule is NOT enforced by construction alone** — unlike
  Gateway/Policy, `Portal` has no automatic re-check of this rule at
  construction time (a known, narrow gap in this particular spec), so
  `build_portal_plan` calls `portal.validate()` explicitly before
  rendering rather than trusting construction to have caught it.
- `resolver` must be a real IP address, never a hostname — nginx
  resolves it itself at startup, and `nginx -t` rejects a name there.
- `endpoint` is a real, computed in-cluster URL with no credential in
  it — quote this rather than guessing a service name.
- Leave `options` unset — it auto-fills from `type` with the correct
  image. `type` is what picks the admin vs. organization UI; setting
  `options` by hand risks pairing one type with the other's
  `image_repository`, which deploys the wrong UI with no error anywhere.
