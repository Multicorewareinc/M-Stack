## What this changes

<!-- One or two sentences. What behaviour is different afterwards? -->

## Why

<!-- What problem this solves. If it fixes a failure that was hard to
diagnose, say what the failure looked like — that context is worth more
than the diff. -->

## How it was verified

<!-- Tests are necessary but rarely sufficient for this SDK, because most
of it talks to a real cluster. Say what you actually ran. -->

- [ ] `pytest` passes locally
- [ ] Verified against a real cluster — say which operations, and what the output was
- [ ] Not verifiable against a cluster, and here is why

## Checklist

- [ ] **Tests cover the new behaviour**, and I checked they fail without the change.
      A test that passes either way is worse than no test.
- [ ] **Errors say what to do next.** An error message is the part of this
      SDK people meet when things go wrong; "invalid value" is not enough.
- [ ] **Comments say what a setting prevents**, not what it is. `docs/structure.md`
      has the house style.
- [ ] **No vendor name in a spec class, example folder or doc filename** —
      `Storage`, not `LonghornStorage`. See `docs/structure.md`.
- [ ] **Fields are in the right place**: generic on the spec, implementation-specific
      in `options`, translation in the driver.
- [ ] **No credentials in a spec.** They come from Kubernetes Secrets, and never
      onto a command line — process arguments are readable by any local user.
- [ ] **`kubeconfig_path` is explicit.** Nothing in this SDK falls back to
      `$KUBECONFIG` or `~/.kube/config`.
- [ ] If I changed a spec, capability or backend name: examples and docs updated.
      `tests/test_examples.py` enforces this, so a stale reference fails `pytest`.
- [ ] If I added a driver: registered in `DRIVERS` *and* `SUPPORTED_TYPES` in the
      same change, never ahead of the driver working.

## Anything you are unsure about

<!-- Where you want a second opinion. Naming it here gets it read. -->
