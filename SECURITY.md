# Security Policy

## Reporting a vulnerability

**Do not open a public issue for anything exploitable.** Report it
privately through GitHub:

<https://github.com/Multicorewareinc/M-Stack/security/advisories/new>

Include what you'd include in a bug report — the operation you ran, the
`check_prerequisites()` output, `spec.model_dump()` with credentials
stripped — plus what makes this one exploitable rather than just broken.

There is no dedicated security team; this is a small team's SDK. Expect
an acknowledgement within a few business days, not an SLA. If it's
urgent and you've heard nothing, say so in the advisory thread.

What happens after a report:

1. Someone with repo access reviews and confirms it, and asks for
   whatever's missing to reproduce it.
2. Severity and fix approach get decided in the advisory thread, which
   stays private until a fix is ready.
3. A fix lands, credited to you unless you ask otherwise, and the
   advisory is published alongside it (or shortly after, if affected
   users need time to update first).

## What's in scope

This SDK composes and shells out to other people's software
(`ssh`/`helm`/`kubectl`) rather than reimplementing it. In scope here is
this repository's own code:

- Anything that could turn a spec's fields, an example script's inputs,
  or a Helm values dict into command injection, path traversal, or
  arbitrary code execution.
- Any path that could put a credential somewhere it doesn't belong — a
  log line, an error message, a command line's arguments (readable by
  any local user via `/proc`), a spec that gets `model_dump()`'d and
  pasted into an issue.
- `multistack/state/`'s SQLite file or `multistack/helm/redact()`
  failing to do what their own docstrings say they do.
- Anything that lets one capability's spec or driver act on a cluster
  other than the one its explicit `kubeconfig_path` names.

## What's out of scope

- Vulnerabilities in what this SDK deploys — RKE2, Longhorn, MinIO,
  vLLM, Valkey, NATS, CloudNativePG, Istio, MetalLB, the NVIDIA/
  Tenstorrent stacks, or any Helm chart it installs. Report those
  upstream.
- Vulnerabilities in `helm`, `kubectl`, `ssh`, or Kubernetes itself.
- A cluster that was already compromised before this SDK touched it, or
  credentials that leaked some other way (a shared shell history, a
  screen share) and were merely used with this SDK afterward.
- Missing hardening that isn't a concrete, exploitable path — this SDK
  is not expected to defend against a root user on a box it's already
  running commands on with that user's own privileges.

## Known, currently open issues

Tracked in [`CHANGELOG.md`](CHANGELOG.md) under `### Security` rather
than hidden — check there before reporting something that's already
known.
