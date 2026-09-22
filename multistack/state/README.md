# State

`StateManager` records the lifecycle and health of everything this SDK
deploys, in a SQLite file. It never provisions anything itself — each
capability's own backend (`RKE2Backend`, `StorageBackend`, `MinIOBackend`,
`IngressGatewayBackend`, `PolicyBackend`, `GatewayBackend`, ...) does the
real work and reports into this layer as it goes, tagging each deployment
with a `component_type` string ("cluster", "storage", "objectstore",
"ingress_gateway", "policy" — the rate limiter, "gateway" — the model
gateway, ...).

This answers a question no per-backend state can: "what is deployed,
across every component, and what shape is it in" — one SQL query instead
of a directory walk over N JSON files. (`RKE2Backend` also keeps its own
JSON under `~/.multistack/state/<cluster>.json` for its own idempotency —
join token, node list — and that is unrelated and untouched by this.)

## Where the database lives

One default, everywhere: `~/.multistack/state.db` — a path a normal user
can always write to, no setup required. Every backend records through
`default_state_manager()` (`multistack/state/tracking.py`), which
resolves the db path from `$MULTISTACK_STATE_DB` if set, else falls
through to `StateConfig`'s own default; `StateManager()`/`StateConfig()`
constructed directly, with no arguments, land on that exact same path —
there is no second, different default to accidentally hit.

Running this rooted as a system service (one `state.db` shared by every
user on the machine) is still available, just opt-in: pass
`StateConfig(db_path="/var/lib/platform/state.db")` (or wherever) to
whichever `StateManager` you construct for that deployment.

## What's recorded

One row per deployment (`Deployment` in `models.py`): `name`,
`component_type`, `status`, `kubeconfig_path`, `error`, timestamps.
`status` is one of `pending, validating, provisioning, healthy, degraded,
failed, deleting`. A separate `health_checks` table keeps a history of
probes (`record_health_check`); the latest one is also folded into
`status`.

## Using this in another component

This is the only change a backend needs — decorate its `create()`:

```python
from multistack.state.tracking import track_create

class MyBackend:
    @track_create("my_capability", name_of=lambda spec: spec.name)
    def create(self, spec, *args, **kwargs):
        ...  # the real work, unchanged
```

* `"my_capability"` is the `component_type` string. It must match
  whatever string a *dependent* spec names in its own `REQUIRES` tuple
  (e.g. `MinIOTenant.REQUIRES = ("cluster", "storage")` expects rows
  tagged `"cluster"` and `"storage"` to exist and be healthy).
* `name_of(spec)` returns the deployment's unique name — `spec.name` for
  a multi-instance spec (a cluster, a tenant), or `spec.type` for a
  single-instance capability spec that has no name of its own (`Storage`,
  `Policy`, `Gateway`, `IngressGateway`).

`track_create` then, around your unchanged `create_method` body:

1. Calls `state.require_healthy_dependencies(spec.REQUIRES)` — raises
   `DependencyResolutionError` **before anything else happens** if a
   required capability has no healthy row yet. Nothing is recorded for
   this deployment when this step blocks — a blocked dependency is not a
   failed attempt at *this* component, so it doesn't get a row.
2. Calls `state.start(name, component_type=...)` — creates the row (or
   reuses an existing one, for a reconcile) and marks it `provisioning`.
3. Runs your real `create()` body.
4. On success: `state.mark_healthy(name, kubeconfig_path=...)`.
   On any exception: `state.mark_failed(name, str(exc))`, then
   **re-raises** — callers see the same exception as before, the state
   layer only observes it.

So a spec's own `REQUIRES` tuple is what wires it into this — declare it
once on the spec (see `multistack/capability.py`), and every backend that
creates that spec gets correct dependency gating for free.

`delete()` gets the same treatment, symmetrically — decorate it with
`@track_delete(name_of=...)`:

```python
from multistack.state.tracking import track_delete

class MyBackend:
    @track_delete(name_of=lambda spec: spec.name)
    def delete(self, spec, *args, **kwargs):
        ...  # the real teardown, unchanged
```

Only touches state for a name this layer already has a row for — deleting
something this process never tracked just runs the real `delete()`
untouched, rather than inventing a row or raising a spurious not-found.
When it does apply: `DELETING` while the real teardown runs, the row is
**removed entirely** on success (once torn down there is nothing left to
call healthy, or even "deleted" — `remove()` is exactly this: "the real
work already succeeded, drop the record"), or kept and marked `FAILED`
with the exception text on any error — a delete that raised may have left
things partly torn down, which is not the same as gone.

`update()` has no equivalent today; a backend that reconciles rather than
replacing would need its own decorator following the same shape.

## Reading state back

```python
from multistack.state import default_state_manager, DeploymentStatus

state = default_state_manager()
state.get("edge-cluster")            # one Deployment, or None
state.list_deployments()             # every row
state.latest_health_check("edge-cluster")
[d for d in state.list_deployments() if d.status == DeploymentStatus.FAILED]
```

## Two failure modes, both observable

* **Gate blocks** — `Storage(...)` created with no healthy `"cluster"`
  row yet: `DependencyResolutionError` raised immediately, **no row** is
  written for the storage deployment.
* **Recorded failure** — dependencies satisfied, but the real operation
  raises (bad SSH port, missing kubeconfig, a failed Helm install, ...):
  the row exists, ends up `status=failed`, and `error` holds the
  exception text, e.g.:

  ```
  longhorn   storage   FAILED   storage requires a Kubernetes cluster, but no
  kubeconfig exists at /home/ubuntu/kubeconfig.yaml. Either the cluster was
  never created, or it was deleted — create it first (see examples/rke2/).
  ```

Tests: `tests/test_state.py` (the store/manager in isolation) and
`tests/test_backend_state_tracking.py` (the `@track_create` seam, driven
with stand-in specs the same way every real backend uses it).
