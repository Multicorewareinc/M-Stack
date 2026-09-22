# Repo structure and how to extend it

Where code goes, and why it goes there.
[`CONTRIBUTING.md`](../CONTRIBUTING.md) covers setup, tests and the
pull-request flow; this file is the map.

One rule governs the whole SDK:

> **The capability is the class. The implementation is data.**

```python
from multistack import Storage

Storage(type="longhorn")
```

A caller asks for a capability and names an implementation once. Everything
downstream of the spec — an example, a higher layer, an agent tool — never
learns which implementation is in use. The day a second block-storage
driver lands, swapping is a one-word change and nothing above the spec is
touched.

`SUPPORTED_TYPES` lists only implementations that actually have a driver.
The SDK carries no registry of intended ones: a roadmap in the code is a
promise we have to keep, and an error naming an implementation nobody can
deploy is worse than one that simply says what exists.

## Where the code is today

**The move to that shape is nearly finished, and this section is the map.**
Thirteen capabilities are in the target shape. Copy `storage/`: it is the
only one that also has volume claims and prerequisite checks, so it
exercises more of the pattern. Two older components still sit in the
`core/` + `backends/` split and work exactly as they did.

| Capability | Spec today | Lives in | Shape |
|---|---|---|---|
| storage | `Storage` | `multistack/storage/` | migrated — copy this one |
| policy | `Policy`, `RateLimits` | `multistack/policy/` | migrated |
| gateway | `Gateway` | `multistack/gateway/` | migrated |
| inference | `Inference` | `multistack/inference/` | migrated |
| ingress_gateway | `IngressGateway` | `multistack/ingress_gateway/` | migrated |
| observability | `Observability` | `multistack/observability/` | migrated |
| tokenizer | `Tokenizer` | `multistack/tokenizer/` | migrated |
| controlplane | `ControlPlane` | `multistack/controlplane/` | migrated |
| portal | `Portal` | `multistack/portal/` | migrated |
| route | `Route` | `multistack/route/` | migrated |
| cache | `Cache` | `multistack/cache/` | migrated |
| accelerator | `Accelerator` | `multistack/accelerator/` | migrated |
| database | `Database`, `DatabaseConfig` | `multistack/database/` | migrated |
| cluster | `RKE2Cluster`, `RKE2Node` | `core/rke2.py`, `backends/rke2_client.py` | older split |
| object store | `MinIOTenant` | `core/minio.py`, `backends/minio_client.py` | older split |

**New capabilities go in the target shape. No exceptions.** `core/` and
`backends/` are the residue of the first architecture, not a second one
you may choose between: the split is closed to new work, and the two
above are what is left to migrate, not a pattern to follow. That rule is
written down because it was broken twice — `core/valkey.py` landed
15 September and `core/cnpg.py` 16 September, both into the older split,
eight and nine days after the target shape existed and four capabilities
were already using it. Each one added that way is one more to migrate
later.

To save you grepping for things that are not there yet: there is no
`Cluster` or `ObjectStore` spec, no `multistack/node.py`, no top-level
`registry.py`, and no `Node` class. The shared node model is `RKE2Node` in
`core/rke2.py`, and the shared node transport is `backends/transport.py`.

Every migrated spec subclasses `CapabilitySpec`, and its backend
subclasses `CapabilityBackend`. The two older specs are plain pydantic
models that declare the same wiring ClassVars by hand, and their backends
mix in `NodeCommandMixin` with no driver dispatch — there is nothing to
dispatch to while a capability has one implementation. `cache` shows what
changes when one moves: `Cache` gained a `type`, a `ValkeyOptions` for
the chart specifics, and a `drivers/valkey.py`, while its endpoint
computation and every behaviour its tests assert stayed put.

What every component shares regardless of shape: `multistack/kube.py`,
`multistack/helm/`, `multistack/stack.py`, `backends/transport.py`, and
the `REQUIRES` / `FROM_STACK` / `PROVIDES` declarations.

## Start here, by task

| If you are… | Read |
|---|---|
| new here, and the layout is not landing yet | [One call, all the way down](#one-call-all-the-way-down) |
| adding a field to an existing spec | [Which fields go where](#which-fields-go-where), then that spec |
| adding an implementation | [Adding an implementation](#adding-an-implementation-to-an-existing-capability), then `storage/` end to end |
| adding a whole capability | [Adding a capability](#adding-a-capability), then `capability.py` |
| composing layers together | [Dependencies](#dependencies-declared-verified-and-wired), then `stack.py` |
| running a cluster command | [Mechanism is shared](#mechanism-is-shared-policy-belongs-to-the-caller), then `kube.py` |
| installing a chart | [`../examples/helm/install.py`](../examples/helm/install.py) |
| writing tests | [Where the seams are](#where-the-seams-are), then a sibling test module |
| debugging a deploy | that backend's `check_prerequisites()` first — it is written to answer exactly this |

## One call, all the way down

The sections below describe the pieces. This one follows a single call
through all of them, because "which file does what" is a much harder
question to answer than "what happens when I run this".

The whole storage flow is four lines:

```python
from multistack import Stack, Storage, StorageBackend

stack   = Stack(kubeconfig_path="~/.multistack/kubeconfig")   # 1
storage = stack.build(Storage, type="longhorn")               # 2
StorageBackend().create(storage, nodes=nodes)                 # 3
stack.record(storage)                                         # 4
```

**1 — `from multistack import …`** lands in `multistack/__init__.py`,
which is re-exports and nothing else. It pulls in the *spec* modules.
`drivers/longhorn.py` does not load here; that matters in step 3.

**2 — `stack.build(Storage, type="longhorn")`** is where the wiring
happens, and it is all in `stack.py` and `capability.py`:

```
stack.py  build()
  |
  |-- _check_order(Storage)
  |     reads   Storage.REQUIRES = ("cluster",)
  |     looks up CAPABILITY_OUTPUT["cluster"] -> "kubeconfig_path"
  |     present in this Stack? yes -> continue
  |     absent? MissingDependencyError naming the layer that owes it
  |
  |-- reads   Storage.FROM_STACK = {"kubeconfig_path": "kubeconfig_path"}
  |     the caller did not pass it, so fill it from the Stack
  |     (had they passed it, theirs wins — explicit beats wired)
  |
  `-- Storage(type="longhorn", kubeconfig_path=...)
        |
        `-- capability.py  _fill_in_and_check()      <- a pydantic validator
              |-- resolve_options()
              |     OPTIONS_FOR_TYPE["longhorn"] -> LonghornOptions
              |     options was None, so options = LonghornOptions()
              `-- validate_capability()
                    type in SUPPORTED_TYPES? namespace non-empty?
                    options the class this type expects?
                    then LonghornOptions.validate()
```

At the end of step 2 you hold a valid `Storage` and **nothing has
contacted the cluster**. That separation is deliberate: a spec is
checkable offline, which is why the test suite runs in under two seconds.

**3 — `StorageBackend().create(storage, nodes=nodes)`** is where it
becomes real. Note how little the capability's own code does:

```
storage/registry.py  create()
  |    return self.driver_for(storage).create(storage, nodes)   <- one line
  |
  `-- capability.py  driver_for()
        |-- spec.validate()
        |
        |-- verify_requirements(spec)
        |     "cluster" in REQUIRES
        |     `-- kube.py  require_cluster()
        |           kubectl --kubeconfig ... version -o json
        |           *** the first cluster contact in the whole flow ***
        |
        |-- driver_class("longhorn")
        |     DRIVERS["longhorn"] = ("multistack.storage.drivers.longhorn",
        |                            "LonghornDriver")
        |     importlib.import_module(...)
        |     *** longhorn.py loads here, not at import time ***
        |
        `-- cache the instance per type, return it

LonghornDriver.create()   helm and kubectl, both through kube.py
```

**4 — `stack.record(storage)`** publishes the one value this layer
produced, so the next layer can be wired to it:

```
stack.py  record()
  reads   Storage.PROVIDES = {"storage_class": "storage_class_name"}
  getattr(storage, "storage_class_name") -> "longhorn"    (a @property)
  Stack._values["storage_class"] = "longhorn"
```

A MinIO tenant then declares `FROM_STACK = {"storage_class":
"storage_class"}` and gets it without anyone repeating the string. That
is the entire handoff mechanism — one dict on the spec, one dict inside
the Stack. There is no container, no injection and no discovery.

Two things in that trace are the ones to remember, because most confusion
about this codebase is really confusion about one of them:

- **A spec validates itself; a backend talks to the cluster.** Construction
  is pure. `driver_for()` is the boundary, and it is crossed once.
- **The `type` string becomes a driver in exactly one place**, in
  `capability.py`. No `if type == "longhorn"` exists anywhere else, which
  is what makes a second implementation a registry entry rather than a
  patch across the tree.

## Layout

What is actually on disk:

```
multistack/
  __init__.py          public surface: import specs and backends from here
  capability.py        CapabilitySpec, CapabilityBackend — shared machinery
  kube.py              cluster primitives: kubectl, apply, wait_for, require_*
  stack.py             Stack: state the shared facts once, thread them down
  py.typed             PEP 561 marker, so callers' type checkers see the types

  core/                specs for the components not yet migrated — closed to new work
    rke2.py            RKE2Cluster, RKE2Node
    minio.py           MinIOTenant
  backends/            the provisioning for those, one module each
    transport.py       NodeCommandMixin: SSH/local exec, probes, fan-out
                       — shared by every capability, both shapes
    rke2_client.py     RKE2Backend, ClusterState
    minio_client.py    MinIOBackend

  storage/             a capability in the target shape — the one to copy
    __init__.py        re-exports only — the package's whole import surface
    spec.py            Storage, LonghornOptions, VolumeClaim
    base.py            StorageDriver Protocol + catchable error types
    registry.py        DRIVERS: type -> (module, class), and StorageBackend
    drivers/
      longhorn.py      LonghornDriver, imported only when type="longhorn"

  policy/              same four files: Policy, RateLimits, RPMOptions
    drivers/rpm.py     RPMDriver — the rate limiter's chart and endpoint
  gateway/             same four files: Gateway, ModelGatewayOptions
    drivers/modelgateway.py
  inference/           same four files: Inference, VLLMOptions
    drivers/vllm.py    VLLMDriver — manifests, prereqs, wait, warmup
  database/            same four files: Database, DatabaseConfig, CNPGOptions
    drivers/cnpg.py    CNPGDriver — two lifecycles, operator and cluster

  helm/                mechanism too, but big enough to be a package
    __init__.py        HelmRunner — a synchronous surface over an async dep
    manager.py         the async layer (pyhelm3)
    client.py          the pyhelm3 adapter
    resolver.py        chart name -> repository, with a parsed-index cache
    values.py          checks values against the chart's values.yaml
    models.py errors.py config.py repositories.py

tests/                 mirrors the source; nothing here touches a cluster
examples/              one folder per component, never per implementation
docs/                  one file per component or topic
```

And the shape a migrated capability takes — four files, not two:

```
multistack/<capability>/
  __init__.py          re-exports only
  spec.py              <Capability> + one <Impl>Options per implementation
  base.py              <Capability>Driver Protocol + catchable errors
  registry.py          DRIVERS, and <Capability>Backend
  drivers/<impl>.py    <Impl>Driver, loaded only when that type is chosen
```

Each file answers one question: *what can I ask for* (`spec.py`), *what
must I implement* (`base.py`), *what exists* (`registry.py`), *how does
one of them work* (`drivers/`). A driver author reads `base.py` and never
`registry.py`; a caller reads neither.

A capability with no working implementation does not get a spec, a package
or a row in any table here until the driver behind it works.

## Drivers load on demand

`DRIVERS` maps a `type` to `(module_path, class_name)`, not to an imported
class:

```python
DRIVERS = {
    "longhorn": ("multistack.storage.drivers.longhorn", "LonghornDriver"),
}
```

`CapabilityBackend.driver_class()` imports the module the first time that
`type` is selected. Three things follow, and they are the reason this is
worth the indirection:

- `import multistack` costs nothing per implementation. Every driver today
  shells out to `ssh`, `helm` and `kubectl` and imports only the standard
  library, but the first one that reaches for a client library must not
  make the whole SDK depend on it.
- A driver with a missing optional dependency fails when it is selected,
  not at `import multistack`. One unusable implementation cannot stop the
  others from working.
- Nothing outside a capability package can casually reach a driver, so
  interchangeability is structural rather than a convention.

The cost is that a typo in `DRIVERS` would otherwise surface only when
someone selects that type. `resolve_all()` imports every entry, and
`tests/test_capability.py` calls it, so a broken registry fails under
`pytest` instead.

A driver class can also be registered directly, for tests and for
registering one at runtime, where there is no module to name.

## Specs are pydantic models

A spec that exists is a spec that validated. `CapabilitySpec` and every
spec under `core/` run their checks in a `model_validator(mode="after")`,
and `validate_assignment=True` runs them again on any later mutation. So
there is no window in which an invalid spec exists:

```python
storage = Storage(type="longhorn", kubeconfig_path=kc)
storage.replica_count = 0                               # at the assignment
Storage(type="gluster", kubeconfig_path=kc)             # at construction
```

Both raise `ValidationError`, which subclasses `ValueError` — so existing
`except ValueError` and `pytest.raises(ValueError)` keep working.

`validate()` is still public, still called by `driver_for()`, and still
reads well at a call site — it just can no longer be the first place a
problem shows up. Tests assert on the constructor for that reason.

What this buys beyond the old hand-written `validate()`:

- **`extra="forbid"`** — `replica_cont=1` is refused rather than ignored.
  A silently-dropped field is how a spec ends up not doing what the file
  says it does.
- **Construction from a mapping**, which is what makes loading a spec from
  JSON or YAML possible: `Storage.model_validate(data)` coerces the nested
  `options` mapping into the right options model and validates on the way
  in, so a hand-edited file cannot smuggle in a spec that fails at deploy
  time.
- **Serialization**, which replaced `ClusterState`'s hand-written
  `asdict`/`RKE2Node(**n)` round-trip. The old version rebuilt nested
  nodes without validating them, so a state file with a bad role loaded
  happily and failed later.

Two things to know before writing one:

**Class constants need `ClassVar`.** pydantic treats an annotated class
attribute as a field, so `SUPPORTED_CNI`, `CAPABILITY`, `SUPPORTED_TYPES`
and friends are all `ClassVar[...]`. Without it they become constructor
arguments and a spec carries its own contract as data. A test in
`tests/test_capability.py` asserts none of them leaked into
`model_fields`, because the failure is otherwise invisible — the spec
still works, it just accepts `CAPABILITY="whatever"`.

This is also why `CapabilitySpec` can be a real base class rather than
the mixin it had to be under dataclasses: pydantic has no rule forcing a
base's fields ahead of a subclass's.

**A method that mutates two fields to reach a valid state must set them
together.** `validate_assignment` fires per assignment, so this fails
halfway:

```python
self.root_user = user          # root_password still None -> ValidationError
self.root_password = password
```

`MinIOTenant.set_credentials()` is the pattern: assign both, then
validate once. Keeping it a single method is what keeps the
"set together or not at all" rule enforceable.

## Dependencies: declared, verified, and wired

Three separate jobs, and it is worth keeping them separate.

### Declared

Every spec carries `REQUIRES`, machine-readable so a composer can order
operations, `docs/<capability>.md` can state the dependency without it
drifting, and the agent layer can refuse an out-of-order plan instead of
discovering the problem mid-deploy.

The dependency is on the **capability**, never an implementation. Storage
needs *a* Kubernetes cluster; a kubeconfig from k3s or a managed provider
satisfies it exactly as well as RKE2's. `multistack/kube.py` therefore
does not import anything under `core/` — coupling one capability to
another's implementation is precisely what this split exists to prevent.

This is the full wiring table as it stands, and the thing to copy when
adding a spec:

| Spec | `REQUIRES` | `FROM_STACK` (field ← key) | `PROVIDES` (key ← attr) |
|---|---|---|---|
| `RKE2Cluster` | `()` — it produces the kubeconfig | `kubeconfig_path` | `kubeconfig_path` |
| `Storage` | `("cluster",)` | `kubeconfig_path` | `storage_class` ← `storage_class_name` |
| `MinIOTenant` | `("cluster", "storage")` | `kubeconfig_path`, `storage_class` | `s3_endpoint_url` ← `endpoint` |
| `Inference` | `("cluster",)` | `kubeconfig_path`, `s3_endpoint_url`, `s3_secret_name` | `inference_endpoint` ← `endpoint` |
| `Policy` | `("cluster",)` | `kubeconfig_path`, `cache_url`, `event_backbone_url` | `policy_endpoint` ← `endpoint` |
| `Gateway` | `("cluster",)` | `kubeconfig_path`, `event_backbone_url`, `api_key_secret` | `gateway_endpoint` ← `endpoint` |

`RKE2Cluster` both takes and publishes `kubeconfig_path`, which is not a
contradiction: going in it is where to *write* the file, coming out it is
where the file *is*. Declaring `PROVIDES` without `FROM_STACK` is what
broke cluster creation under a `Stack` once — `build()` left the path
`None` and nothing was written.

### Verified

Against the actual cluster, before any work starts. `kubeconfig_path` is
a string, so it can name a cluster that was deleted an hour ago — and
that failure otherwise surfaces as `x509: certificate signed by unknown
authority` from inside a Helm call, minutes later.

`multistack/kube.py` has `require_cluster` (the file exists, is readable,
and an API server answers) and `require_storage_class` (a usable class
exists). `CapabilityBackend.driver_for()` calls `verify_requirements()`
before dispatching, and the base version runs `require_cluster` for any
spec whose `REQUIRES` names `cluster` and `require_storage_class` for
`storage`. So a capability in the target shape inherits both, including
one nobody has written yet.

A backend in the older shape inherits nothing, and has to call them
itself — `MinIOBackend` does it in `check_prerequisites`. If you add to
`backends/`, that is on you to remember; it is one of the reasons the
migrated shape is worth finishing.

Only `cluster` is verifiable generically — a reachable API server means
the same thing to everyone. Anything else is capability-specific, so
override `verify_requirements()`, call `super()`, and add yours. MinIO's
own check is the example worth reading: it distinguishes "names a class
that does not exist" (fatal) from "names none and there is no default"
(a warning, since something else may still provision), which is more than
the generic check can know.

Declaring is not verifying, and the gap used to be real: only `Storage`
checked anything, so MinIO and vLLM took a `kubeconfig_path` and never
confirmed it pointed anywhere.

### Wired

By `Stack` — see [`multistack/stack.py`](../multistack/stack.py):

```python
stack = Stack(kubeconfig_path=kc)

storage = stack.build(Storage, type="longhorn")
StorageBackend().create(storage, nodes=nodes)
stack.record(storage)                      # publishes its StorageClass

tenant = stack.build(MinIOTenant, name="minio", servers=2)
#  -> kubeconfig_path and storage_class both filled in
```

`build()` fills only what the caller left out, so an explicit argument
always wins, and it fills *before* constructing — so the spec's own
validation still sees a complete set of values and a spec still cannot
exist in a state it would reject.

`build()` also refuses to construct a spec whose `REQUIRES` name a
capability that has published nothing:

```
MinIOTenant requires the 'storage' capability, but nothing has published
'storage_class' to this Stack.

Create that layer first, then stack.record(its_spec). Published so far:
['kubeconfig_path'].
```

That is the part `field required` cannot tell you: not that a value is
missing, but which layer was supposed to produce it.

**A Stack is not ambient**, and the distinction is the whole point. This
SDK refuses to read `$KUBECONFIG` or `~/.kube/config` because a stale
default silently targets the wrong cluster. A Stack is the opposite: one
explicit statement of which cluster, at the top of the file, threaded
down, with `stack.outputs` showing exactly what will be filled in.
Specs stay standalone — `Storage(type="longhorn", kubeconfig_path=...)`
works with no Stack anywhere, and every example except `full_stack.py`
still does that.

`record()` goes *after* the backend, never before. A StorageClass name is
knowable from the spec before anything exists, and publishing it early
would let the next layer bind against a class that is not installed yet —
which is exactly what `require_storage_class` exists to catch.

## Mechanism is shared; policy belongs to the caller

`multistack/kube.py` holds the primitives for talking to a cluster:

```python
require_cluster(kubeconfig)              # the declared cluster dependency
require_cli("helm", error_cls=MyError)   # a CLI is on PATH
run_local(argv, error_cls=MyError)       # a local CLI, stdin closed, typed errors
kubectl(kubeconfig, "get", "nodes")      # always --kubeconfig scoped
apply(kubeconfig, objects)               # piped to `kubectl apply -f -`
wait_for(check, timeout=...)             # poll until true, raise on timeout
wait_for_job(kubeconfig, name, ns)       # a Job, with its logs on failure
node_name_for(kubeconfig, ip)            # our node identity -> Kubernetes'
```

Each of these existed three or four times before — in the MinIO backend,
the vLLM backend, the Longhorn driver, and again in every example — and
the copies had drifted. Two closed stdin and two did not, so two could
hang until timeout on a CLI that decided to prompt. Two turned a timeout
into a typed error and two let `subprocess.TimeoutExpired` escape, so
`except MinIOError` silently missed timeouts. Three presence checks did it
three ways, one interpolating a binary name into `bash -c` unquoted.

None of that was a design decision; it was four people solving the same
problem on different days. The rule that prevents the next round:

> **If every capability needs it and it makes no decisions, it is
> mechanism. If it decides something, it belongs to the caller.**

`multistack/helm/` is mechanism too, and the reason it is a package rather
than one file is size, not shape: no `spec.py`, no `registry.py`, no
`drivers/`, because Helm has no interchangeable implementations for a
caller to choose between. The test for a capability is whether a user
picks between implementations, and nobody picks between Helms.

It also carries the SDK's one async dependency. `HelmRunner` is
synchronous and the async layer stops behind it, so no driver and no
example ever writes `await` — see
[`../examples/helm/install.py`](../examples/helm/install.py).

Running `kubectl` is mechanism. *Which* node to label, what to call a
Secret, which bucket the weights go in — those are policy, and they stay
in the composition that chose them.
[`examples/full_stack.py`](../examples/full_stack.py) is the test of the
line: it imports the mechanism and keeps only the decisions.

Two consequences worth stating. Every capability passes its own
`error_cls`, so callers keep catching `StorageError` or `MinIOError` while
sharing one implementation underneath — consolidating the mechanics does
not flatten the error types. And because every cluster call routes through
`kubectl()`, which always passes `--kubeconfig`, "no ambient
`$KUBECONFIG`" is enforced in one place rather than being a rule each
backend has to remember.

## What a backend looks like

The verbs are deliberately the same across components, so knowing one
tells you most of the next:

| Method | Where | Does |
|---|---|---|
| `check_prerequisites(spec[, nodes])` | all four | returns a list of warnings; raises only on a fatal, fixable-on-a-node problem |
| `create(spec[, nodes])` | all four | built to be re-runnable — a provisioning script that already ran must not fail on the second pass |
| `delete(spec)` | all four | tears down; what it also removes (PVCs, for instance) is an explicit argument, never a default |
| `install_prerequisites(spec, nodes)` | storage | the node-side packages that driver needs (open-iscsi, and so on) |
| `endpoint(spec)` | MinIO, vLLM | the URL callers actually use |
| `update(spec)` | RKE2 | diffs the spec against what it provisioned — adding and removing nodes and bumping versions are all this one call |
| `create_claim`, `resize_claim`, `delete_claim` | storage | PVCs, on the capability rather than per driver |

`check_prerequisites()` is the one to reach for when a deploy failed. It
is written to be the first thing anyone runs, and returning warnings
rather than raising is deliberate: most problems are worth reporting
without refusing to proceed.

## Where the seams are

Backends keep a thin `_local` / `_kubectl` / `_helm` of their own over the
shared primitives. That is deliberate: it is one seam per backend for
tests to intercept, and it is how "no cluster command is ever unscoped" is
actually asserted rather than merely intended.

So a test patches that one method and asserts on the argv it was handed —
`tests/backends/` and `tests/storage/` are full of the pattern. Nothing in
`tests/` touches a cluster, a node or the network, which is why the whole
suite runs in a couple of seconds and why it cannot prove that a real
deploy works. See [`CONTRIBUTING.md`](../CONTRIBUTING.md) on what to run
against real machines before opening a pull request.

`tests/test_examples.py` is a different kind of guard: it parses every
example and every ```python snippet in the docs, resolves their imports,
checks markdown links resolve and checks the paths named in examples
exist. It is what makes a half-finished rename fail rather than rot.

## Errors are catchable without naming an implementation

Every implementation's errors subclass the capability's, declared in
`base.py`:

```python
class LonghornError(StorageError): ...
class LonghornPrerequisiteError(StoragePrerequisiteError): ...
```

So a caller writes `except StorageError` and stays implementation-agnostic
on the failure path too — which also means the error types are importable
without loading the driver that raises them. `StoragePrerequisiteError` is
separate because the fix is on a node, not in the spec.

One level further up, the capability errors themselves subclass the
transport's `NodeCommandError` / `NodePrerequisiteError`, so
`except NodeCommandError` catches a failure from any component. Use the
narrowest one that says what you mean.

Drivers themselves are not re-exported from a capability package. Naming
`LonghornDriver` gives up the interchangeability the package exists to
provide, and importing one eagerly would defeat the lazy loading above.

## Naming

Every example below is real code you can go and read, all of it from the
one migrated capability:

| Thing | Rule | Example |
|---|---|---|
| Capability folder | lowercase capability noun | `storage/` |
| Spec class | the capability, no vendor | `Storage` |
| Backend class | `<Capability>Backend` | `StorageBackend` |
| Driver contract | `<Capability>Driver` (a `Protocol`) | `StorageDriver` |
| Driver module | the implementation | `storage/drivers/longhorn.py` |
| Driver class | `<Impl>Driver` | `LonghornDriver` |
| Options class | `<Impl>Options` | `LonghornOptions` |
| Test module | mirrors the source | `tests/storage/test_longhorn.py` |

Never put a vendor name in a spec class or an example folder.
`Inference`/`examples/inference/` follows this: a second engine
(llama.cpp, TGI) adds a `type` and a driver, not a new folder. (Some
older vendor-named example folders predate the rule — see
[Known gaps](#known-gaps).)

## Which fields go where

- **On the spec** — anything that means the same thing to every
  implementation. `replica_count`, `model`, `cpu_cores`. These are what a
  caller reasons about.
- **In `options`** — anything specific to one implementation.
  `kubelet_root_dir` is a Longhorn CSI quirk; `kv_cache_gb` is a vLLM
  concept. Typed, and validated against `type`, so a Longhorn-only setting
  handed to another implementation fails loudly rather than being ignored.
- **In the driver** — the translation from generic spec to that
  implementation's actual interface. Only `LonghornDriver` knows that
  `replica_count` is spelled `defaultSettings.defaultReplicaCount`.

If a field has to be renamed to make sense for a second implementation, it
belonged in `options`.

Credentials are in none of those places. They come from Kubernetes
Secrets, and never onto a command line — process arguments are readable by
any local user.

## Adding an implementation to an existing capability

1. `<Impl>Options` in `spec.py`, with its own `validate()`.
2. Add it to `SUPPORTED_TYPES` and `OPTIONS_FOR_TYPE` — in the same
   change as the driver, never ahead of it.
3. `drivers/<impl>.py` with `<Impl>Driver`, satisfying the capability's
   `Protocol` in `base.py`.
4. One entry in `DRIVERS`, in `registry.py`.
5. `tests/<capability>/test_<impl>.py`.

Nothing else changes — no caller, no example, no other capability. That
property is the point of the shape.

## Adding a capability

Copy `multistack/storage/`; it is the shape, and it is small enough to
read in one sitting.

1. `<capability>/spec.py` — the spec subclassing `CapabilitySpec`,
   declaring `CAPABILITY`, `SUPPORTED_TYPES`, `OPTIONS_FOR_TYPE`,
   `DEFAULT_NAMESPACES` and `REQUIRES` as `ClassVar`, plus `FROM_STACK`
   and `PROVIDES` if it takes part in a `Stack`. Options defaulting and
   validation are inherited; add field validators for your own rules.
2. `<capability>/base.py` — the `<Capability>Driver` `Protocol` and the
   capability's error types.
3. `<capability>/drivers/<impl>.py` — a driver.
4. `<capability>/registry.py` — `DRIVERS`, and a backend subclassing
   `CapabilityBackend`.
5. `<capability>/__init__.py` — re-export the spec, backend and errors,
   and nothing else.
6. Re-export from `multistack/__init__.py`, the stable public surface.
7. `tests/<capability>/`, `examples/<capability>/`, `docs/<capability>.md`.
8. Add the capability's published output to `CAPABILITY_OUTPUT` in
   `stack.py`, or `build()` cannot check ordering against it.

Roughly 40 lines of declaration plus the driver. The generic parts — type
validation, options matching, namespace defaulting, dispatch, caching —
come from `capability.py` and are not reimplemented.

## Two modelling decisions already made

**Block storage and object storage are separate capabilities.** You cannot
back a PVC with MinIO, so they are not substitutable, and one `type` across
both would make the choice meaningless. The split also lets a system that
provides both appear under each as its own `type`, independently. The test
for "is this one capability or two" is *substitutability*, not topic.

**CNI selection stays on the cluster spec, not a `Networking`
capability.** RKE2 bundles the CNI and fixes it at cluster creation — it
cannot be swapped on a running cluster, and the kube-proxy chart config
has to be staged before the first server starts. So `cni` stays where it
is. `Networking` is reserved for what genuinely installs *after* a cluster
exists: ingress controllers, load balancers, network policy. Modelling CNI
as a post-cluster capability would promise a swap the platform cannot
perform.

## Mistakes this codebase has actually made

Not hypotheticals — each of these shipped, and each is cheap to repeat:

- **A `type` registered ahead of its driver.** `SUPPORTED_TYPES` accepted
  something nothing could deploy. The guard in `tests/test_capability.py`
  now requires `SUPPORTED_TYPES` and `DRIVERS` to match exactly.
- **`PROVIDES` without `FROM_STACK`.** `build()` left `kubeconfig_path`
  unset, no file was written, and every later layer failed to find a
  cluster.
- **A published value the implementation does not create.**
  `storage_class_name` returned `type` while the driver named the release
  — identical by default, and a StorageClass that does not exist as soon
  as anyone overrode the release name. PVCs stuck `Pending`.
- **Status matched as a string.** `wait_for_job` compared against `"1"`,
  so a Job with `backoffLimit > 1` was never seen to fail and blocked for
  the full timeout with its logs never surfaced. Parse the numbers.
- **A jsonpath escape that Python ate first.** `"{'\t'}"` in a non-raw
  string is a literal tab, and the query silently matched nothing —
  reported as "no StorageClass at all" on a cluster with two. Prefer
  `-o json` and parse it.
- **Two-field mutation, one at a time.** Covered above; it fails halfway
  under `validate_assignment`.
- **A doc left describing the target instead of the code.** That is what
  most of this file's last revision was.

## Shared, not per-capability

- **`RKE2Node`** — an SSH-reachable machine, in `core/rke2.py`. Storage and
  inference take lists of it too, which is why the target shape calls it
  `Node` at the top level: naming shared infrastructure after one
  implementation is what the whole rule exists to avoid. `role` is
  optional and only the cluster backend reads it.
- **`backends/transport.py`** — `NodeCommandMixin`: SSH/local execution,
  probes, parallel fan-out, `BatchMode=yes` so an unreachable host fails
  instead of hanging. Every backend and driver that runs node commands
  mixes it in.
- **`kube.py`, `helm/`, `stack.py`, `capability.py`** — the machinery
  above.

These live at the top level, not under any capability. A driver importing
`from ..other_capability.something` is a sign something shared is in the
wrong place.

## Known gaps

Stated rather than hidden, because each one is a place the rules above and
the code disagree:

- **The migration is partial.** Two components (cluster, object store)
  are still in the `core/` + `backends/` split, so they have no driver
  dispatch, no `options`, and no `base.py` contract. `cache` and
  `database` both came out on 17 September — they were the two that had
  been *added* to the split after the target shape existed, which is why
  the rule against that is written down above rather than assumed.
  `rke2` and `minio` are what remain: the widest blast radius and the
  least structural gain, since `RKE2Node` and `transport.py` are
  load-bearing for every other capability.
- **Vendor-named example folders.** `examples/rke2/` and `examples/minio/`
  predate the naming rule. They get renamed with the capabilities they
  document, not before — `tests/test_examples.py` checks the links, so a
  rename has to be complete.
- **No CI.** `pytest` locally is the only gate today.
