# Contributing

Thanks for wanting to work on this. One thing is worth saying before the
mechanics, because it shapes everything else here:

> **This SDK provisions real infrastructure, and its test suite cannot
> prove that it does.**

The suite runs in under two seconds precisely because none of it
touches a cluster. They check specs, validation, dispatch and the shape of
the commands the SDK builds — every real failure this project has had so
far got through them. So a change here needs both: tests that fail
without it, *and* a sentence saying what you ran against a live cluster.

By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## Setting up

Python 3.10 or newer.

```bash
python -m venv venv && source venv/bin/activate
pip install -e ".[dev,helm]"
pytest
```

`dev` adds pytest and the packaging tools; `helm` adds `pyhelm3`, needed
only by `multistack/helm/`. Without it the Helm tests skip rather than
fail — see `skip_if_optional_extra` in `tests/test_examples.py`.

`pip` cannot install what the backends actually shell out to. For anything
beyond running the tests you also need, per [`README.md`'s
Prerequisites](README.md#prerequisites):

- `kubectl` — every cluster-side operation
- `helm` — the chart-based components (Longhorn, MinIO)
- `ssh` — provisioning remote nodes, with key-based login and
  passwordless `sudo` on each one

## The one rule

> **The capability is the class. The implementation is data.**

```python
Storage(type="longhorn")
```

A caller asks for a capability and names an implementation once. Nothing
downstream of the spec learns which one is in use.
[`docs/structure.md`](docs/structure.md) is the full explanation, and the
two things it will save you rewriting:

- **Where a field goes.** Generic on the spec, implementation-specific in
  a typed `options`, translation in the driver. If a field would have to
  be renamed to make sense for a second implementation, it belonged in
  `options`.
- **The numbered recipes** for adding an implementation to an existing
  capability (five steps) and adding a capability (five more). The
  generic parts — type validation, options matching, namespace
  defaulting, dispatch, caching — come from `multistack/capability.py`
  and should not be reimplemented.

Three consequences that catch people:

**A new capability goes in its own package — never in `core/` +
`backends/`.** `multistack/<capability>/` with `spec.py`, `base.py`,
`registry.py`, `drivers/` and `__init__.py`, the spec subclassing
`CapabilitySpec` and the backend `CapabilityBackend`. The `core/` +
`backends/` split is the residue of the first architecture and is
**closed to new work** — the two components still in it are what is
left to migrate, not a shape to copy. This is written down because it
was missed twice: `core/valkey.py` and `core/cnpg.py` both landed there
over a week after the target shape existed, and each one added that way
is one more to migrate later. Copy `multistack/storage/`.

**Never put a vendor name in a spec class, an example folder or a doc
filename.** `Storage`, not `LonghornStorage`.

**Register a `type` in the same change as its driver, never ahead of
it.** A roadmap in the code is a promise, and an error naming an
implementation nobody can deploy is worse than one that says what
exists. `tests/test_capability.py` enforces the match between
`SUPPORTED_TYPES` and `DRIVERS`, and imports every registry entry.

## Tests

```bash
pytest                              # all of it, ~2s
pytest tests/storage -q             # one capability
pytest tests/test_examples.py -q    # the example and doc guards
```

- **Check your test fails without your change.** A test that passes
  either way is worse than no test — it costs the same to run and reports
  nothing. All thirteen findings in `aaf4543` were outside test
  coverage, and every one was confirmed by running the code rather
  than by running the suite.
- **Assert on the constructor.** Specs are pydantic models, so
  validation runs at construction and again on assignment;
  `validate()` still exists but can no longer be the first place a
  problem shows up.
- **Mirror the source layout.** `tests/<capability>/test_<impl>.py`.
- **Renames must be complete.** `tests/test_examples.py` parses every
  example and every ```python doc snippet, resolves their imports,
  checks markdown links and checks the paths named in examples exist. A
  half-finished rename fails there, which is the point.

## Verifying against a cluster

Most of this SDK is only provable this way, so the pull-request template
asks for it directly. What is useful in a PR:

- which operations you ran, and against what — node count, CNI, GPU or
  not
- the output that shows it worked, or the output that showed the bug
  before the fix
- if you could not verify it live, say so and why

Some failures only exist on real hardware: an MTU that only breaks under
an encapsulating CNI, a multi-homed node picking the wrong address, a
GPU below vLLM's compute-capability floor. None of those are reachable
from a unit test.

There is no CI yet, so `pytest` locally is currently the only gate.
Please run it.

## Style

The house style is in the code and in
[`docs/structure.md`](docs/structure.md); these are the parts reviewers
ask for most.

- **Comments say what a setting prevents, not what it is.**
  `cilium_mtu=1350` is not "the MTU" — it is the value that stops pods
  from hanging on large responses under encapsulation. The second form
  survives someone deciding to tidy it away.
- **Error messages say what to do next.** They are the part of this SDK
  people meet when things are already going wrong. "invalid value" is
  not enough; name the field, the constraint and the fix.
- **Credentials never live in a spec**, and never on a command line —
  process arguments are readable by any local user via `/proc`. They come
  from Kubernetes Secrets.
- **`kubeconfig_path` is always explicit.** Nothing here falls back to
  `$KUBECONFIG` or `~/.kube/config`, because a stale default silently
  targets the wrong cluster.
- Wrap prose and comments at 76 columns, matching the existing files.

## Commits and pull requests

Work targets `staging`. `main` holds releases.

Commit subjects are imperative and specific — "Write the kubeconfig as
soon as the first server answers", not "fix kubeconfig". The body is
where the value is: what was broken, what it looked like when it broke,
and why the fix is shaped the way it is. If a failure was hard to
diagnose, that description is worth more than the diff.

One change per pull request, and fill in
[the template](.github/PULL_REQUEST_TEMPLATE.md) — its checklist is the
same list a reviewer would otherwise write out by hand.

## Reporting a bug

Use the issue templates. The bug one asks for the output of
`check_prerequisites()` and of `spec.model_dump()`; between them those
answer most storage and inference reports without a round trip. **Strip
credentials before pasting a spec.**

## Security

Do not open a public issue for anything exploitable. Report it privately
at
<https://github.com/Multicorewareinc/M-Stack/security/advisories/new>.

Known, currently unfixed issues are listed under `### Security` in
[the changelog](CHANGELOG.md) — they are stated rather than hidden, so
please check there before reporting.

## Licensing

This project is Apache-2.0. Contributions are accepted under that same
license, as Apache-2.0 §5 provides by default. There is no CLA and no
separate sign-off to add.
