# multistack

A declarative Python SDK for provisioning AI infrastructure. Every
capability follows the same pattern — a declarative spec object plus a
backend that does the actual work — so the stack is composed rather than
hand-assembled: provision a cluster, give it storage, then run workloads on
top.

## Install

Two ways to get it, depending on what you're doing:

**Using the SDK against your own machines** — install the published
package, no clone needed:

```bash
pip install multistack-sdk
```

It's currently pre-release (`0.x`, still under initial testing) and
published to TestPyPI rather than the real index, so today that's actually:

```bash
pip install --pre --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ multistack-sdk
```

`--pre` is required — pip hides pre-release versions from a plain install
by design — and `--extra-index-url` is required because TestPyPI doesn't
mirror real PyPI, so `pydantic` won't resolve without it. Once this lands
on real PyPI, the plain `pip install multistack-sdk` above is all it takes.

**Working on the SDK itself** — editable install from a clone, so source
changes take effect immediately:

```bash
pip install -e .
```

Either way, the distribution is **`multistack-sdk`**; the import name is
`multistack`. Note that `multistack` on PyPI is an unrelated, abandoned
OpenStack client wrapper whose last release was in 2015 — `pip install
multistack` will succeed and give you the wrong package, with no error to
tell you so.

Python 3.10+ and `pydantic`. Specs are pydantic models, so a spec that
exists is one that validated — and one can be built from a dict, which is
what lets a spec be loaded from YAML or JSON.

Beyond that the backends shell out to `ssh`/`helm`/`kubectl` rather than
using client libraries, so no Kubernetes or cloud SDK comes with it. See
the per-backend docs for anything backend-specific (SSH access, external
CLIs, etc).

The Helm layer's own dependency is an optional extra, so an RKE2-only
install doesn't fetch it:

```bash
pip install -e ".[helm]"     # adds pyhelm3 (which still needs helm on PATH)
```

## Prerequisites

Beyond the `pip install` above, actually deploying anything needs:

- **The infrastructure itself, already provisioned and network-reachable.**
  This SDK configures RKE2, storage, and everything above it on machines
  you provide — it does not provision bare metal, VMs, or the network
  between them. Every node needs L3 reachability to every other node,
  including UDP: the default CNI (Cilium) overlays pod traffic over VXLAN,
  and a network that allows TCP but blocks UDP between nodes lets the
  cluster come up looking healthy and then breaks all pod-to-pod
  networking with nothing in the RKE2 logs to explain why. If the path MTU
  between nodes is below the CNI's default (1450 for Cilium), set
  `cilium_mtu` accordingly at cluster-creation time — get this wrong and
  traffic to the affected node fails in ways that look like anything
  except MTU.
- **`ssh`, `kubectl` and `helm` on `PATH`.** The backends shell out to
  these rather than using client libraries — `helm` only matters once
  you install the `[helm]` extra.
- **Non-interactive SSH to every node, key-based, with either root or
  passwordless `sudo -n`.** RKE2 installation and node provisioning run
  as a series of unattended SSH commands; a sudo prompt or a password
  prompt has nothing there to answer it.
- **Run the SDK from the first control-plane node itself** —
  `MULTISTACK_SERVER` below — not from a separate operator machine.

## Pointing it at your machines

No addresses are hardcoded. The scripts in `examples/` read your lab from
the environment, and their defaults are RFC 5737 documentation addresses
that cannot route anywhere — so an unconfigured example fails to connect
rather than reaching someone else's host.

```bash
export MULTISTACK_SERVER=10.0.0.10                    # the RKE2 server
export MULTISTACK_AGENTS=10.0.0.11,10.0.0.12          # agents, comma-separated
export MULTISTACK_SSH_USER=ubuntu
export MULTISTACK_SSH_KEY=~/.ssh/id_ed25519
export MULTISTACK_KUBECONFIG=~/.multistack/kubeconfig # where the kubeconfig lands

python examples/rke2/cluster.py
```

The SDK never reads an ambient `$KUBECONFIG` or `~/.kube/config` —
every cluster-consuming component takes the path explicitly, so it can
only act on the cluster you named.

### Running it from a plain `pip install`, with no `examples/` on disk

`examples/` is excluded from the published package (`pyproject.toml`'s
`[tool.setuptools.packages.find]` leaves it out on purpose, so the
distribution ships code, not a copy of the repo) — so if you only ran
`pip install multistack-sdk` above, `python examples/rke2/cluster.py`
isn't there to run. Write the same handful of lines yourself instead:

```python
# cluster.py — save this, fill in your machines, run it.
import os
from multistack import RKE2Cluster, RKE2Node
from multistack.backends.rke2_client import RKE2Backend

SERVER = os.environ["MULTISTACK_SERVER"]         # the RKE2 server node
AGENTS = os.environ.get("MULTISTACK_AGENTS", "").split(",")
SSH_USER = os.environ.get("MULTISTACK_SSH_USER", "ubuntu")
SSH_KEY = os.environ.get("MULTISTACK_SSH_KEY", "~/.ssh/id_ed25519")
KUBECONFIG = os.path.expanduser(
    os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig")
)

cluster = RKE2Cluster(
    name="ai-cluster",
    version="v1.32.5+rke2r1",
    nodes=[
        RKE2Node(address=SERVER, user=SSH_USER, role="server", ssh_key=SSH_KEY),
        *[RKE2Node(address=a.strip(), user=SSH_USER, role="agent", ssh_key=SSH_KEY)
          for a in AGENTS if a.strip()],
    ],
    kubeconfig_path=KUBECONFIG,
)

kubeconfig = RKE2Backend().create(cluster)
print(f"Cluster '{cluster.name}' created — kubeconfig at {kubeconfig}")
```

```bash
export MULTISTACK_SERVER=10.0.0.10
export MULTISTACK_AGENTS=10.0.0.11,10.0.0.12
python3 cluster.py
```

Run this **on the target first control-plane node itself** — the same
`MULTISTACK_SERVER` — not from a separate operator machine, per
Prerequisites above. See [`examples/rke2/cluster.py`](examples/rke2/cluster.py)
in the repo — the fuller version of this same script, and the reference
for the rest of `RKE2Cluster`'s fields (CNI choice, `pin_node_ip`,
`cilium_mtu`, and so on) — for anyone working from a clone instead.

## Capabilities

Each its own package under `multistack/`, listed roughly in the order a
full build brings them up — later ones depend on earlier ones
(`REQUIRES`), and `Stack` (below) enforces that order rather than
letting a capability build before what it needs exists.

| Capability | Docs | What it does |
|---|---|---|
| RKE2 | [`examples/rke2/cluster.py`](examples/rke2/cluster.py) | Provisions RKE2 clusters — nodes, join tokens, CNI selection |
| Accelerator | [`examples/accelerator/install.py`](examples/accelerator/install.py) | Advertises a GPU as a schedulable resource (NVIDIA device plugin) |
| Ingress Gateway | [`examples/ingress_gateway/install.py`](examples/ingress_gateway/install.py) | One external LAN address for the whole cluster (MetalLB + Istio) |
| Storage | [`examples/storage/install.py`](examples/storage/install.py) | Installs distributed block storage (Longhorn today), providing the StorageClass a fresh RKE2 cluster lacks |
| MinIO | [`examples/minio/tenant.py`](examples/minio/tenant.py) | Deploys MinIO tenants — S3-compatible object storage for model weights and datasets |
| Queue | [`examples/queue/install.py`](examples/queue/install.py) | The async event backbone (NATS/JetStream) that rate limiting and metering read from |
| Inference | [`examples/inference/serve.py`](examples/inference/serve.py) | Serves a model behind an OpenAI-compatible endpoint (vLLM or llama.cpp), on CPU or GPU |
| Cache | [`examples/cache/create.py`](examples/cache/create.py) | An in-cluster key-value counter store (Valkey) |
| Database | [`examples/database/create_operator.py`](examples/database/create_operator.py) | A PostgreSQL cluster via CloudNativePG |
| Tokenizer | [`examples/tokenizer/install.py`](examples/tokenizer/install.py) | Counts tokens for a response whose upstream reported none |
| Enricher | [`examples/enricher/install.py`](examples/enricher/install.py) | Guarantees a usable token count on every gateway response event |
| Policy | [`examples/policy/install.py`](examples/policy/install.py) | Requests- or tokens-per-minute rate limiting in front of a model |
| Gateway | [`examples/gateway/install.py`](examples/gateway/install.py) | An authenticated OpenAI-compatible front door, in front of inference |
| Billing | [`examples/billing/install.py`](examples/billing/install.py) | Usage metering and Stripe subscriptions |
| Control Plane | [`examples/controlplane/install.py`](examples/controlplane/install.py) | The platform's own admin/organization API |
| Portal | [`examples/portal/install.py`](examples/portal/install.py) | The web UI served in front of a control plane |
| Route | [`examples/route/install.py`](examples/route/install.py) | Attaches a service to the ingress gateway (Gateway API `HTTPRoute`) |
| Observability | [`examples/observability/install.py`](examples/observability/install.py) | Prometheus, Alertmanager and Grafana |
| Helm | [`examples/helm/install.py`](examples/helm/install.py) | Installs, upgrades and uninstalls Helm releases — the layer every chart-based capability sits on |

Composing them — the cluster's kubeconfig is the handoff between layers.
This is the earliest few; [`examples/full_stack.py`](examples/full_stack.py)
below is the same idea carried through every capability in the table:

```python
kc = RKE2Backend().create(cluster)                      # 1. a cluster

StorageBackend().create(                                # 2. block storage,
    Storage(type="longhorn", kubeconfig_path=kc),        #    which RKE2 lacks
    nodes=cluster.nodes,
)

tenant = MinIOBackend().create(                         # 3. object storage
    MinIOTenant(kubeconfig_path=kc, name="minio", storage_class="longhorn")
)                                                       #    on top of it

endpoint = InferenceBackend().create(                   # 4. inference, fed
    Inference(                                          #    from the tenant
        kubeconfig_path=kc,
        model="s3://models/Qwen2.5-0.5B-Instruct",
        s3_endpoint_url=tenant.endpoint,
        s3_secret_name="minio-creds",
    ),
    nodes=cluster.agent_nodes,
)
```

Each layer's output is the next one's input: the kubeconfig threads through
all four, Longhorn provides the StorageClass MinIO's PVCs bind to, and
MinIO's endpoint is where vLLM reads its weights — so the model is served
without the pod ever reaching the internet.

[`examples/full_stack.py`](examples/full_stack.py) is that chain as a
runnable script, bare machines to a served model, including the glue the
components deliberately don't model (buckets, node labels, Secrets).

Every cluster-consuming component takes `kubeconfig_path` explicitly rather
than reading ambient `$KUBECONFIG`/`~/.kube/config`, so it can only act on
the cluster you named — a stale default kubeconfig otherwise targets a
cluster that may no longer exist.

## State tracking

Every capability's `create()`/`update()`/`delete()` records into
[`multistack/state/`](multistack/state/__init__.py) as it goes — what's
deployed, what shape it's in, and, on a failed create, what went wrong.
Backed by one SQLite file, `~/.multistack/state.db` by default
(`StateConfig` points it elsewhere). It's a pure recorder: nothing here
provisions anything, and nothing reads it back to decide what to do — it
exists so "what's DEGRADED right now" is a query instead of a walk over
every component's own state.

## Repo layout

```
multistack/
  <capability>/             # one package per capability — spec.py, base.py,
    spec.py                 # registry.py, drivers/, __init__.py (see docs/structure.md)
    base.py
    registry.py
    drivers/
  core/ + backends/         # the original shape, closed to new work — RKE2Cluster
                             # and MinIOTenant are what's left to migrate out of it
  helm/                     # the Helm layer every chart-based capability sits on
  stack.py                  # Stack: composes capabilities, threads kubeconfig_path,
                             # enforces REQUIRES/PROVIDES ordering
tests/                       # mirrors the package layout: tests/<capability>/
examples/
  full_stack.py             # every capability end to end, bare machines to a served model
  <capability>/              # one folder per capability: install, update, delete, ...
docs/
  structure.md              # the capability pattern and how to extend it
```

Everything is organized one-per-capability, so adding one means adding a
package (or an `examples/` folder), not editing a shared file —
[`docs/structure.md`](docs/structure.md) is the full shape and the
numbered recipe for extending it; each capability's own example script
under `examples/<capability>/` is the reference for using it, in place of
a separate doc per capability. The `__init__.py` re-exports
(`from multistack import RKE2Cluster, Storage, Gateway, ...`) are the
stable public surface — import from there, not from individual modules.

## Running the tests

```bash
pip install -e ".[dev]"   # or just: pip install pytest
pytest
```

They stub out everything that shells out (SSH/subprocess), so they need no
cluster, no nodes, and no network. Add tests alongside the module they
cover, mirroring the source layout: `tests/<capability>/` for most of
them, `tests/core/`/`tests/backends/` for the two not yet migrated out
of that split.

## Building the SDK

Editable install for local development (source changes take effect immediately, no reinstall needed):

```bash
pip install -e .
```

The distribution is **`multistack-sdk`**; the import name is `multistack`.
Note that `multistack` on PyPI is an unrelated, abandoned OpenStack client
wrapper whose last release was in 2015 — `pip install multistack` will
succeed and give you the wrong package, with no error to tell you so.

Building a distributable wheel + sdist:

```bash
pip install build          # or: pip install -e ".[dev]"
python -m build             # produces dist/multistack_sdk-<version>-py3-none-any.whl and dist/multistack_sdk-<version>.tar.gz
```

Verify the built artifacts before publishing anywhere:

```bash
pip install twine           # or: pip install -e ".[dev]"
twine check dist/*
```

Installing the built wheel elsewhere, to confirm it actually works outside your dev checkout:

```bash
pip install dist/multistack_sdk-0.1.0-py3-none-any.whl
```

Publishing (if/when this goes to a package index):

```bash
twine upload dist/*                       # PyPI
twine upload --repository testpypi dist/* # or TestPyPI first, to sanity-check
```

Bumping the version means editing `version = "0.1.0"` in `pyproject.toml`
— there's no separate `__version__` string elsewhere to keep in sync.

## Contributing

[`CONTRIBUTING.md`](CONTRIBUTING.md) covers setup, where a new field or
implementation goes, and what a pull request needs. The short version:
the tests are fast because none of them touch a cluster, so a change
wants both a test that fails without it and a sentence about what you ran
against real machines.

Everyone taking part is held to the
[Code of Conduct](CODE_OF_CONDUCT.md). Anything exploitable goes to
[a private advisory](https://github.com/Multicorewareinc/M-Stack/security/advisories/new),
never a public issue.

## Known limitations (SDK-wide)

- **Tests cover logic, not real provisioning.** `tests/` exercises the
  spec model, state persistence, diffing and command construction with
  everything that shells out stubbed, so nothing verifies a real cluster
  actually comes up — `examples/rke2/verify.py` against real machines is
  still the only end-to-end check.
- **No CLI.** Everything is Python-API-only right now.

See each capability's own example script under `examples/<capability>/`
for anything specific to that capability — comments there call out
known gotchas (network/MTU requirements, unsupported values, and so on)
in place of a separate doc per capability.
