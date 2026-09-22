# Changelog

Notable changes to this SDK. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Nothing has been released yet, so everything below is unreleased and the
public surface is still free to move.

## [Unreleased]

### Added

- **The capability pattern.** The capability is the class and the
  implementation is data: `Storage(type="longhorn")` rather than
  `LonghornStorage`. `multistack/capability.py` holds the shared
  machinery — type validation, typed `options` matched against `type`,
  namespace defaulting, driver dispatch and caching — so a new capability
  declares its choices and its own fields rather than reimplementing any
  of it. `docs/structure.md` describes the shape and how to extend it.
- **`multistack.stack.Stack`**, a composition root. States which cluster
  once, explicitly, and threads it into every spec; publishes what each
  layer produced for the next (`record()`), and refuses to build a spec
  whose `REQUIRES` name a capability that has published nothing. Not
  ambient — nothing is read from the environment, and `stack.outputs`
  shows exactly what will be filled in. Specs remain usable standalone.
- **Declared dependencies.** Every spec carries `REQUIRES`:
  `RKE2Cluster ()`, `Storage ("cluster",)`,
  `MinIOTenant ("cluster", "storage")`, `VLLMService ("cluster",)`.
  Machine-readable, so a composer can order operations.
- **Volume claims** — `VolumeClaim` plus `StorageBackend.create_claim`,
  `resize_claim`, `delete_claim`. Implemented on the capability, not per
  driver, since a claim names a StorageClass and every implementation
  produces one. Refuses to shrink, refuses a class without
  `allowVolumeExpansion`, and waits for `Bound` by default.
- **A Helm layer** at `multistack/helm/`. `HelmRunner` is synchronous; the
  async dependency stops behind it, so no driver or example writes
  `await`. Ships `redact()`, atomic upgrades by default, chart-name
  resolution with a cached index, and a values check that reports unknown
  paths rather than refusing them.
- `HelmRunner.get_release()` — the same lookup as `status()`, returning
  `None` instead of raising when there is no such release. From the
  `helm-manager-sdk` branch (Nikhil Rai, 89869a7), reimplemented on the
  direct revision lookup rather than a full release listing.
- **Shared cluster primitives** in `multistack/kube.py`: `require_cluster`,
  `require_storage_class`, `require_cli`, `run_local`, `kubectl`, `apply`,
  `wait_for`, `wait_for_job`, `wait_for_phase`, `wait_for_absent`,
  `node_name_for`.
- `VLLMService.runtime_class`, needed for GPU serving on RKE2 before CDI —
  without `runtimeClassName: nvidia` the pod schedules and then finds no
  GPU.
- Guards for the things nothing was checking: every example and every
  ```python doc snippet must parse and its imports must resolve; markdown
  links and paths named in examples must exist; `SUPPORTED_TYPES` must
  match `DRIVERS`; every registry entry must import.
- `py.typed`, so consumers' type checkers see the annotations at all.

### Changed

- **Specs are pydantic models**, not dataclasses. A spec that exists is one
  that validated: checks run at construction and again on assignment.
  `validate()` remains and is still called by backends, but can no longer
  be the first place a problem shows up. Unknown field names are refused,
  and a spec can be built from a mapping — which is what makes loading one
  from JSON or YAML possible. `pydantic>=2.7` is now the SDK's one hard
  dependency; everything else still shells out to `ssh`/`helm`/`kubectl`.
- **The distribution is `multistack-sdk`.** `multistack` on PyPI is an
  unrelated, abandoned OpenStack client wrapper (last release 2015). The
  import name is unchanged.
- `RKE2Backend.create()` writes the kubeconfig as soon as the first server
  answers, rather than after every node has joined — so the cluster can be
  watched while agents are still joining.
- Parallel batches report per-node elapsed time, so a slow node reads as a
  slow node rather than as work that was never parallel.
- Longhorn moved from `core/` + `backends/` to `multistack/storage/`,
  split into `spec.py` (what to ask for), `base.py` (what to implement),
  `registry.py` (what exists) and `drivers/`. Drivers load on demand, so
  `import multistack` costs nothing per implementation.
- Errors subclass a capability-level type (`StorageError`), so failures are
  catchable without naming an implementation.

### Fixed

- `RKE2Cluster` under a `Stack` left `kubeconfig_path` unset, so the file
  was never written and every later layer failed to find it.
- `Storage.storage_class_name` returned `type` while the driver creates
  `options.release_name` — overriding the release name published a
  StorageClass that did not exist.
- `wait_for_job` matched status strings, so a Job with `backoffLimit > 1`
  was never detected as failed and blocked for the full timeout with its
  logs never surfaced.
- `capability.py` called `getattr(options, "validate")`, which finds
  pydantic's deprecated `BaseModel.validate` — following the documented
  recipe for adding a capability raised a `TypeError`.
- `check_prerequisites(spec)` raised `TypeError` when `nodes` was omitted,
  despite being documented as optional.
- `HelmRunner.is_deployed` treated any Helm error as "not deployed", so an
  unreachable cluster looked like a clean slate.
- A missing Helm release was logged with `LOGGER.exception`, so the
  ordinary check-then-install path wrote a stack trace on every first
  install — the noise that hides a real failure. Absence is now a debug
  line with no traceback. Spotted on the `helm-manager-sdk` branch.
- `HelmRunner.is_deployed` decided absence by finding "was not found" in
  the error message, because `_call()` had already re-typed the exception
  to a component's own error class. Any other failure carrying that
  phrase read as a clean slate, and the caller acted on it by installing.
  Absence is now established below the re-typing and matched as `None`.
- `ChartResolver` stopped at the first unreachable repository, hiding every
  chart in the repositories after it.
- Longhorn's `deleting-confirmation-flag` is now set before uninstalling;
  without it Longhorn's uninstaller never runs and the namespace is left
  Terminating behind CRDs and finalizers.
- `MinIOBackend` and `VLLMBackend` verify their declared cluster
  dependency; previously neither confirmed the kubeconfig pointed
  anywhere, and a stale one surfaced as an x509 error from inside a helm
  call minutes later.
- `MinIOTenant` credentials move together via `set_credentials()`; setting
  them one at a time tripped the set-together rule halfway.
- `examples/rke2/cluster.py` was missing `pin_node_ip` and `cilium_mtu`,
  so running it rebuilt a configuration already known to be broken on
  multi-homed nodes.
- Stale references throughout: a user-facing error pointing at a deleted
  doc, examples naming their own former paths, and `kubectl -n None`.
- `HelmManager._redact_values` carried its own, smaller copy of the
  sensitive-key set than `multistack.helm.redact()` — missing exactly
  the two keys a MinIO tenant's values use, `secretKey` and `accessKey`
  — so the DEBUG-level install/upgrade log lines that called it leaked
  them in cleartext. It now delegates to `redact()` instead of carrying
  a second, drifting copy.
- `examples/full_stack.py` passed MinIO root credentials as `kubectl`
  arguments, readable by any local user via `/proc`. The Secret is now
  built as a manifest and piped to `kubectl` on stdin.

### Security

- The two credential-leak paths previously listed here are now fixed —
  see Fixed, above.
- See [`SECURITY.md`](SECURITY.md) for how to report anything like this
  privately.

### Known limitations

- The migration to the capability layout is partial. `docs/structure.md`
  describes the target; `core/` and `backends/` still hold the cluster,
  object-storage and inference components in the older shape.
- No CI. The test suite, example guards and doc guards run only locally.
