"""Whole-stack tool -- composes RKE2 + (optionally) Storage +
(optionally) MinIO + (optionally) a GPU Accelerator + (optionally)
Inference (model serving) + (optionally) Valkey + (optionally) CNPG
(PostgreSQL) + (optionally) Observability (metrics/dashboards/alerting)
+ (optionally) a Tokenizer + (optionally) an Enricher (guarantees a
token count on every response event) + (optionally) a Model Gateway +
(optionally) a rate-limiting Policy (one or more) + (optionally) an
Ingress Gateway + (optionally) a ControlPlane (one or more) +
(optionally) a Portal (one or more) + (optionally) Billing (usage
metering + Stripe) + (optionally) one or more Routes into ONE script,
using the real SDK's Stack object to wire the layers together (see
multistack/stack.py and examples/full_stack.py). For "build me a whole
platform" requests, not just one resource; see mcp_tools/rke2.py,
storage.py, minio.py, accelerator.py, inference.py, valkey.py, cnpg.py,
observability.py, tokenizer.py, enricher.py, gateway.py, policy.py,
ingress_gateway.py, controlplane.py, portal.py, billing.py, route.py
for adding one layer to infrastructure that already exists.

PRECEDENCE: when a request names one capability that has a real,
structural dependency on another (per the platform's own request-path
and event-backbone architecture -- see docs/architecture.md), this tool
composes the WHOLE chain together rather than the one component alone.
Concretely: a `type="tpm"` policy or `billing` without a composed
`enricher` is REJECTED outright, not merely built incomplete (see the
dependency-check paragraph below) -- both would otherwise deploy
healthy and silently count or meter nothing, since neither computes
anything itself any more. Where the dependency is a real Stack-tracked
value rather than a yes/no existence check, it is wired automatically:
`gateway.upstream_url` from a composed `inference`, and
`gateway.policy_endpoints` from every composed `policy`, are both new
as of this precedence pass -- previously this tool would render valid
Python for a Model Gateway sitting in front of nothing, requiring a
second call once the real values were known.

`gateway` itself is REJECTED without an organization ControlPlane
composed alongside it, for the same reason: ADR-026 removed static-key
auth, so every /v1 call is verified synchronously against it, and a
gateway built without one is deployed, healthy, and rejects every real
request. This is the platform's own documented baseline -- Postgres,
Redis, the organization control plane, and the gateway, in that order,
required for literally any request-path feature -- and it is enforced
here as ONE rule rather than four, because composing a ControlPlane
already requires a real database and cache (see that dependency check
below): requiring the control plane cascades the whole chain for free.
This is deliberately narrower than "every feature requires the
gateway": a `type="rpm"`/`type="tpm"` policy or `billing` composed
without a `gateway` is NOT rejected, since attaching a rate limiter (or
billing) to a gateway that already exists elsewhere -- or building the
limiter first and wiring the gateway to it in a later call -- is a
legitimate, already-documented workflow this tool supports standalone,
and neither Policy nor Billing's own SDK-level REQUIRES names Gateway
as a dependency the way ControlPlane's REQUIRES names database and
cache.

Database.database is the SAME kind of REAL-VALUE-ONLY field as
Gateway.api_key_secret -- see mcp_tools/cnpg.py's SECURITY note.
`database.password` is a real plaintext password (a known, interim SDK
limitation, not something this tool invents or improves on); never let
the model guess one. This tool has no capability that produces a
database, so nothing ever excludes any `cnpg` field besides
`kubeconfig_path`.

Cache.PROVIDES a real `cache_url` (see mcp_tools/valkey.py) -- so
unlike Gateway/Policy (which never auto-link to each other), a
composed Policy's `cache_url` IS wired in automatically from a
composed `valkey` layer, the same way Inference's `s3_endpoint_url`
wires in from a composed `minio` layer. See the SCOPE NOTE below for
exactly which field is a placeholder under which condition.

SCOPE NOTE on kubeconfig_path/storage_class: Storage.kubeconfig_path,
MinIOTenant.storage_class/kubeconfig_path, Inference.kubeconfig_path,
Gateway.kubeconfig_path, Policy.kubeconfig_path, and
IngressGateway.kubeconfig_path are REQUIRED fields on those types (so
build_storage_plan/build_minio_plan/build_inference_plan/
build_gateway_plan/build_policy_plan/build_ingress_gateway_plan can
validate a real, already-known cluster). But a cluster this tool is ALSO
building has no kubeconfig_path yet -- Stack fills it in for real, from
the RKE2 layer's own output, once the generated script actually runs.
So for this tool only, whatever value is passed for kubeconfig_path on
any of these types is a REQUIRED placeholder, never used:
_render_script drops it entirely and lets Stack supply the real value
at run time instead. Pass any non-empty string for it.

Inference.s3_endpoint_url joins kubeconfig_path as a second REQUIRED
placeholder, but only conditionally: only when `minio` is ALSO composed
in this same call AND `inference.model` is an `s3://` path. Inference's
own validate() requires a real-looking s3_endpoint_url the instant an
s3:// model is built -- that check runs eagerly, before this tool's own
code ever sees the object -- so the caller still has to pass some
non-empty string for it, exactly like kubeconfig_path; it is then
discarded the same way, since MinIOTenant.PROVIDES will publish the
real one once recorded. Outside that specific combination (no minio,
or a non-s3 model), s3_endpoint_url is a real field like any other, and
whatever the caller passes (including None, its default) is rendered
as-is.

Policy.cache_url is a THIRD placeholder case, simpler than
s3_endpoint_url's: it's a placeholder whenever `valkey` is ALSO
composed in this same call, unconditionally -- there's no equivalent
of "is this model an s3:// path" gating it, since a Policy always
wants a real cache regardless of its other settings. Policy's own
validate() requires a real-looking cache_url immediately (same eager
construction-time check as s3_endpoint_url), so the caller still
passes some non-empty string; it is discarded and Stack fills in the
real one from the recorded Cache (Cache.PROVIDES = {"cache_url":
"endpoint"}). Without `valkey` composed, cache_url is a real field
like any other and whatever the caller passes is rendered as-is.

Gateway.org_cp_internal_url is REAL-VALUE-ONLY too, and deliberately
not in Gateway.FROM_STACK: a Stack records whichever ControlPlane was
recorded last and cannot tell an admin one from an organization one,
and only the ORGANIZATION control plane can verify a bearer token. So
even when `controlplane` is composed in this same call, this is never
wired from it -- the caller passes the real URL.

Gateway.api_key_secret/event_backbone_url and Inference.s3_secret_name
are also in their spec's FROM_STACK (see multistack/stack.py), but
unlike kubeconfig_path (and s3_endpoint_url/cache_url under the
conditions above) they are NEVER placeholders here -- nothing this
tool builds produces a Secret or an event backbone, so those stay real
values you must supply yourself, exactly as build_gateway_plan/
build_inference_plan already require standalone. IngressGateway.address_pool
has no FROM_STACK entry at all -- it's always a real, caller-supplied
fact about the network, never a placeholder.

This tool DOES wire a composed Inference's endpoint into a composed
Gateway's upstream_url, and every composed Policy's endpoint into a
composed Gateway's policy_endpoints -- even though Gateway.FROM_STACK
declares neither, so the real SDK's Stack itself has no generic rule
for either. Both are the request path's own arrows (Gateway calls
Inference; Gateway calls each rate limiter's /check synchronously), so
this tool wires them explicitly, the same way tokenizer -> enricher is
wired despite having no FROM_STACK entry either. `upstream_url` follows
the placeholder convention (a required field, always overridden when
`inference` is composed); `policy_endpoints` follows "explicit beats
wired" instead (a `[]`-default field, filled only when the caller left
it untouched). Without the corresponding layer composed, a real value
is still required standalone, or a second build_gateway_plan call once
one exists elsewhere. Ingress Gateway has no such link: it provisions
the front door itself and does not route any other service's traffic
through it -- `route` is the capability for that, and it IS auto-wired
to a composed `ingress` (see the IMPORTANT paragraph below).

Observability.storage_class/kubeconfig_path are BOTH placeholders,
unconditionally, whenever `observability` is composed at all -- this
capability REQUIRES storage the same way MinIO does (see
mcp_tools/observability.py), and this tool only allows composing it
alongside a composed `storage` layer, so its storage_class placeholder
never depends on a conditional the way s3_endpoint_url/cache_url do.

Tokenizer has only ONE FROM_STACK entry (kubeconfig_path). It used to
wire into a `type="tpm"` Policy directly, but ADR-030 moved token
counting into the Enricher: the enricher guarantees a count and
republishes to gateway.events.enriched, the tpm limiter consumes that
enriched stream, and `TPMOptions.tokenizer_url`/`tokenizer_timeout_ms`
were DELETED from the SDK rather than deprecated. The consumer is now
the Enricher, and this tool wires `tokenizer` -> `enricher.options.
tokenizer_url` accordingly (see mcp_tools/tokenizer.py / the tokenizer
SKILL.md). Without a composed `enricher`, a composed `tokenizer` is an
independent layer like observability -- still deployed, still
publishing `tokenizer_url` to the Stack, consumed by nothing.

A `type="tpm"` Policy REQUIRES a composed `enricher` in this same call
(unless `policy.options.event_stream_subject` has been explicitly
pointed somewhere other than the enriched default -- a real,
driver-honored override, see multistack/policy/drivers/tpm.py); a
`type="rpm"` one does not, since it reads the raw gateway.events stream
directly. `billing` REQUIRES a composed `enricher` too, unconditionally
-- its consumer subject isn't even configurable the way tpm's is.
Neither requirement is optional the way MinIO's storage one technically
could be worked around some other way: both capabilities are deployed,
healthy, and silently do nothing without it.

ControlPlane.kubeconfig_path is the only placeholder on that type --
`existing_secret` is a REAL-VALUE-ONLY field like Gateway.api_key_secret
and Database.database (see mcp_tools/controlplane.py's SECURITY note): this
tool builds no Secret, so the caller must supply the real name of one
that already exists on the cluster. ControlPlane.REQUIRES = ("cluster",
"database", "cache"), enforced here as a real dependency check: composing
`controlplane` requires BOTH a composed `cnpg` (with `cnpg.database`
set) and a composed `valkey` in the same call. This is an ordering
check only, like MinIO's storage requirement -- database_url/cache_url
are never wired into ControlPlane's own fields, because
ControlPlane.FROM_STACK doesn't declare either (they are deliberately
credential-free at the Stack level, so they cannot serve as the real
DATABASE_URL/REDIS_URL the Secret named by existing_secret must hold).

Portal.kubeconfig_path is always a placeholder. Portal.api_upstream
joins it, but ONLY when `controlplane` is ALSO composed in the same
call -- Portal.FROM_STACK maps api_upstream <- controlplane_endpoint,
so Stack fills it in for real from the composed ControlPlane. Unlike
Gateway/Policy, `Portal` has no model_validator re-running its own
validate() at construction (a narrow SDK gap -- see
mcp_tools/portal.py), so this tool calls `portal.validate()` explicitly
before rendering, same as Database's validate_cluster() check. When
`controlplane` and `portal` are BOTH composed, their `type` fields must
match ("admin" with "admin", "organization" with "organization") --
mismatched types are rejected before rendering, since the auto-wiring
would otherwise silently point a portal at the wrong control plane's
endpoint.

Billing.kubeconfig_path is the only placeholder on that type --
`existing_secret` is REAL-VALUE-ONLY, same reasoning as ControlPlane's.
`billing` REQUIRES BOTH a composed `cnpg` (with `cnpg.database` set,
same as ControlPlane) AND a composed `enricher` (see above) -- database
because it has its own Postgres schema and migration Job, enricher
because its consumer only ever drains gateway.events.enriched.

Route.kubeconfig_path is always a placeholder; `ingress_address` joins
it, but ONLY when `ingress` is ALSO composed (`route` REQUIRES it --
this tool refuses `route` without `ingress` outright, since a route
goes THROUGH the front door rather than creating one). Like Portal,
`Route` has no model_validator re-running its own validate() at
construction, so this tool calls it explicitly. `route` accepts a list
the same way `policy`/`controlplane`/`portal` do, since a real platform
routes several services through one front door; each gets its own
script variable named after the route, and two sharing a `name` are
rejected the same way two policies sharing a `type` are.

Accelerator.kubeconfig_path is always a placeholder, its only
FROM_STACK entry. It has no REQUIRES beyond the cluster and no
PROVIDES -- what it changes is a node property, not an address, so
nothing here reads a value back from it. It is still rendered before
`inference` in the script (see the layer-ordering note above), since a
`device="gpu"` Inference pod needs `nvidia.com/gpu` to already be a
schedulable resource."""

from __future__ import annotations

from typing import List, Optional, Union

from pydantic import validate_call

from multistack import (
    Billing,
    ControlPlane,
    Database,
    Enricher,
    Gateway,
    Inference,
    MinIOTenant,
    Observability,
    Policy,
    Portal,
    Queue,
    RKE2Cluster,
    Route,
    Storage,
    Tokenizer,
    Cache,
)
from multistack.accelerator import Accelerator
from multistack.ingress_gateway import IngressGateway

from mcp_server import mcp
from mcp_tools._script import check_script, dump
from mcp_tools.architecture import DEPENDS_ON, REASON
from mcp_tools.policy import admin_cp_key_error


def _layer_names(label, items):
    """`["policy"]` for one, `["policy:rpm", "policy:tpm"]` for several."""
    if len(items) == 1:
        return [label]
    return [f"{label}:{item.type}" for item in items]


def _field_lines(model, *, exclude: frozenset[str] = frozenset()) -> str:
    """`    field=value,` lines from dump(model), skipping any
    field named in `exclude` (typically a spec's own FROM_STACK keys --
    see the module docstring)."""
    return "\n".join(f"    {k}={v!r}," for k, v in dump(model).items() if k not in exclude)


PLACEHOLDER = "wired"


def prepare_args(args: dict) -> dict:
    """Fills the fields this tool wires itself, when the model left them
    out, before the SDK types validate them.

    The SDK requires some of these at construction (a gateway's
    org_cp_internal_url, an enricher's event_backbone_url) even though the
    script replaces them. Told to pass a placeholder, the model often
    didn't, and each omission cost a rejected call and a model turn -- one
    live request spent three of its four on exactly that. Only empty or
    missing fields are filled; an explicit value is left alone.
    """
    args = {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v) for k, v in args.items()}

    def items(name):
        value = args.get(name)
        if isinstance(value, dict):
            return [value]
        if isinstance(value, list):
            return [v for v in value if isinstance(v, dict)]
        return []

    def fill(spec: dict, field: str) -> None:
        if not spec.get(field):
            spec[field] = PLACEHOLDER

    for name in ("storage", "minio", "inference", "valkey", "cnpg", "observability", "queue",
                 "tokenizer", "enricher", "accelerator", "gateway", "policy", "ingress",
                 "controlplane", "portal", "billing", "route"):
        for spec in items(name):
            fill(spec, "kubeconfig_path")
    for spec in items("minio"):
        fill(spec, "storage_class")
    controlplanes = items("controlplane")
    for spec in items("gateway"):
        if any(cp.get("type") == "organization" for cp in controlplanes):
            fill(spec, "org_cp_internal_url")
        if items("inference"):
            fill(spec, "upstream_url")
    if items("valkey"):
        for spec in items("policy"):
            fill(spec, "cache_url")
    if items("queue"):
        for name in ("gateway", "policy", "enricher", "billing"):
            for spec in items(name):
                fill(spec, "event_backbone_url")
    if len(controlplanes) == 1:
        for spec in items("portal"):
            fill(spec, "api_upstream")
    return args


def _missing_dependency(composed: dict, *, opted_out_of_backbone: dict) -> str | None:
    """An error naming every architecture dependency (see
    mcp_tools/architecture.py) this plan is missing, or None.

    Names the whole missing chain, not just the first gap: told only that
    a tokenizer needs an enricher, the model found the rest one rejection
    at a time, then asked the user whether to include them at all. The
    explicit allow_no_backbone opt-out -- the SDK's own, for deploying
    ahead of the queue on purpose -- waives only the queue."""
    present = {name for name, value in composed.items() if value}

    def needs(name):
        return [
            dep for dep in DEPENDS_ON.get(name, ())
            if not (dep == "queue" and opted_out_of_backbone.get(name))
        ]

    first = next(
        ((name, dep) for name in DEPENDS_ON if name in present for dep in needs(name) if dep not in present),
        None,
    )
    if first is None:
        return None

    # Everything the composed components need, transitively, that isn't here.
    missing, frontier = set(), [n for n in present]
    while frontier:
        for dep in needs(frontier.pop()):
            if dep not in present and dep not in missing:
                missing.add(dep)
                frontier.append(dep)

    name, dep = first
    reason = REASON.get((name, dep))
    # The only values a missing component needs from the user; every other
    # field has a default or is wired. Without this the model asked about
    # the enricher's ports and retry counts.
    from_user = {
        "cnpg": "name, database.name, database.owner, database.password",
        "valkey": "name",
        "controlplane": "existing_secret (a Secret name)",
        "gateway": "api_key_secret (a Secret name), upstream_url (the model server's URL, unless inference is composed)",
        "billing": "existing_secret (a Secret name)",
        "storage": "existing_storage_class, the StorageClass already on an existing cluster",
        "ingress": "address_pool",
    }
    asks = [f"{m}: {from_user[m]}" for m in sorted(missing) if m in from_user]
    labels = {
        "controlplane": "controlplane (type=\"organization\" for a gateway)",
        "storage": "storage (on an existing cluster: existing_storage_class)",
        "cnpg": "cnpg (with cnpg.database)",
    }
    return (
        f"{name} needs {dep} in this plan"
        + (f" -- {reason}" if reason else "")
        + ". The platform's dependency chain means this plan must also include: "
        + ", ".join(labels.get(m, m) for m in sorted(missing))
        + ". Add all of them and call again -- do not ask the user whether to include them."
        + (" The only values to get from the user, if you don't have them: " + "; ".join(asks) + "." if asks else "")
        + " Everything else has a default or is wired -- don't ask about it."
    )


def _render_script(
    cluster: Optional[RKE2Cluster],
    kubeconfig_path: Optional[str],
    existing_storage_class: Optional[str],
    storage: Optional[Storage],
    minio: Optional[MinIOTenant],
    inference: Optional[Inference],
    valkey: Optional[Cache],
    cnpg: Optional[Database],
    observability: Optional[Observability],
    queue: Optional[Queue],
    tokenizer: Optional[Tokenizer],
    enricher: Optional[Enricher],
    accelerator: Optional[Accelerator],
    gateway: Optional[Gateway],
    policies: List[Policy],
    ingress: Optional[IngressGateway],
    controlplanes: List[ControlPlane],
    portals: List[Portal],
    billing: Optional[Billing],
    routes: List[Route],
) -> str:
    if cluster is not None:
        node_lines = "\n".join(
            "            RKE2Node(" + ", ".join(f"{k}={v!r}" for k, v in node.items()) + "),"
            for node in dump(cluster)["nodes"]
        )
        cluster_lines = "\n".join(
            f"    nodes=[\n{node_lines}\n    ]," if field == "nodes" else f"    {field}={value!r},"
            for field, value in dump(cluster).items()
        )
        imports = [
            "from multistack import RKE2Cluster, RKE2Node, Stack",
            "from multistack.backends.rke2_client import RKE2Backend",
        ]
        body = [
            "cluster = RKE2Cluster(",
            cluster_lines,
            ")",
            "backend = RKE2Backend()",
            "kubeconfig = backend.create(cluster)",
            "",
            "stack = Stack(kubeconfig_path=kubeconfig)",
        ]
    else:
        # An existing cluster: the Stack starts from its kubeconfig, and
        # from its StorageClass when it has one, so the layers that
        # require storage build as if a storage layer had been recorded.
        seed = f"kubeconfig_path={kubeconfig_path!r}"
        if existing_storage_class:
            seed += f", storage_class={existing_storage_class!r}"
        imports = ["from multistack import Stack"]
        body = [f"stack = Stack({seed})"]

    # Which organization control plane the gateway verifies keys against,
    # and which admin one a policy reads plans from -- built further down,
    # so wired from each spec's own endpoint (type, namespace and port).
    org_cp = next((cp for cp in controlplanes if cp.type == "organization"), None)

    if storage is not None:
        imports.append("from multistack import Storage, StorageBackend")
        storage_lines = _field_lines(storage, exclude=frozenset(Storage.FROM_STACK) | {"options"})
        if storage.options is not None:
            imports.append("from multistack.storage import LonghornOptions")
            opt_args = ", ".join(f"{k}={v!r}" for k, v in dump(storage.options).items())
            storage_lines += f"\n    options={type(storage.options).__name__}({opt_args}),"
        body += [
            "",
            "storage = stack.build(",
            "    Storage,",
            storage_lines,
            ")",
            "storage_backend = StorageBackend()",
            "storage_backend.install_prerequisites(storage, cluster.nodes)",
            "storage_backend.create(storage, nodes=cluster.nodes)",
            "stack.record(storage)",
        ]

    if minio is not None:
        imports.append("from multistack import MinIOTenant")
        imports.append("from multistack.backends.minio_client import MinIOBackend")
        minio_lines = _field_lines(minio, exclude=frozenset(MinIOTenant.FROM_STACK))
        body += [
            "",
            "tenant = stack.build(",
            "    MinIOTenant,",
            minio_lines,
            ")",
            "minio_backend = MinIOBackend()",
            "minio_backend.create(tenant)",
            "stack.record(tenant)",
        ]

    if accelerator is not None:
        # Before inference, deliberately: this makes nvidia.com/gpu a
        # resource a pod can request, and a device="gpu" Inference
        # created first would sit Pending until it existed.
        imports.append("from multistack.accelerator import Accelerator, AcceleratorBackend")
        accelerator_lines = _field_lines(accelerator, exclude=frozenset(Accelerator.FROM_STACK) | {"options"})
        if accelerator.options is not None:
            options_cls = type(accelerator.options).__name__
            imports.append(f"from multistack.accelerator import {options_cls}")
            opt_args = ", ".join(f"{k}={v!r}" for k, v in dump(accelerator.options).items())
            accelerator_lines += f"\n    options={options_cls}({opt_args}),"
        body += [
            "",
            "accelerator = stack.build(",
            "    Accelerator,",
            accelerator_lines,
            ")",
            "accelerator_backend = AcceleratorBackend()",
            "accelerator_backend.create(accelerator)",
        ]

    if inference is not None:
        imports.append("from multistack import Inference, InferenceBackend")
        # kubeconfig_path is always a placeholder (Stack fills it for
        # real). s3_endpoint_url joins it as a SECOND placeholder, but
        # only when minio is also composed AND model is an s3:// path --
        # Inference's own validate() requires a real-looking
        # s3_endpoint_url the instant an s3:// model is constructed (it
        # runs eagerly, before this function ever sees the object), so
        # unlike kubeconfig_path the caller can't simply leave it unset
        # here; they pass any non-empty string to satisfy that check,
        # and -- exactly like kubeconfig_path -- it's discarded rather
        # than rendered, since MinIOTenant.PROVIDES will publish the
        # real one once recorded. Excluding it whenever minio merely
        # happens to be present, regardless of whether this Inference
        # wants S3 weights at all, previously made an otherwise-valid,
        # non-s3 Inference fail the real SDK's own validation instead.
        #
        # s3_secret_name is NEVER a placeholder -- unlike
        # s3_endpoint_url, nothing in this script creates a Secret
        # (MinIOTenant.PROVIDES has no entry for it), so it stays a
        # real, caller-supplied value here exactly as
        # build_inference_plan already requires standalone, same
        # reasoning as Gateway.api_key_secret/Policy.cache_url.
        exclude = {"kubeconfig_path", "options"}
        if minio is not None and inference.model_is_s3:
            exclude.add("s3_endpoint_url")
        inference_lines = _field_lines(inference, exclude=exclude)
        if inference.options is not None:
            imports.append("from multistack.inference import VLLMOptions")
            opt_args = ", ".join(f"{k}={v!r}" for k, v in dump(inference.options).items())
            inference_lines += f"\n    options=VLLMOptions({opt_args}),"
        body += [
            "",
            "inference = stack.build(",
            "    Inference,",
            inference_lines,
            ")",
            "inference_backend = InferenceBackend()",
            "inference_endpoint = inference_backend.create(inference)",
            "stack.record(inference)",
        ]

    if valkey is not None:
        imports.append("from multistack import Cache")
        imports.append("from multistack.cache import CacheBackend")
        # Only kubeconfig_path is a placeholder here -- Cache has no
        # other FROM_STACK entry, so every other field is a real value
        # like any other. stack.record(cache) is NOT a no-op: Cache
        # DOES have a real PROVIDES (cache_url) -- see the composed
        # policy block below for the layer that actually consumes it.
        cache_lines = _field_lines(valkey, exclude={"kubeconfig_path", "options"})
        if valkey.options is not None:
            imports.append("from multistack.cache import ValkeyOptions")
            opt_args = ", ".join(
                f"{k}={v!r}" for k, v in dump(valkey.options).items())
            cache_lines += f"\n    options={type(valkey.options).__name__}({opt_args}),"
        body += [
            "",
            "cache = stack.build(",
            "    Cache,",
            cache_lines,
            ")",
            "cache_backend = CacheBackend()",
            "cache_backend.create(cache)",
            "stack.record(cache)",
        ]

    if cnpg is not None:
        imports.append("from multistack import Database")
        if cnpg.database is not None:
            imports.append("from multistack import DatabaseConfig")
        imports.append("from multistack.database import DatabaseBackend")
        # Only kubeconfig_path is a placeholder -- database is a real
        # value like Gateway.api_key_secret, never invented here (see
        # mcp_tools/cnpg.py's SECURITY note). Nothing agentic wraps yet
        # consumes database_url, so this is composable but not
        # auto-linked to anything, same starting point the cache had
        # before Policy.cache_url wiring landed.
        cnpg_lines = _field_lines(
            cnpg, exclude={"kubeconfig_path", "database", "options"})
        if cnpg.options is not None:
            imports.append("from multistack.database import CNPGOptions")
            opt_args = ", ".join(
                f"{k}={v!r}" for k, v in dump(cnpg.options).items())
            cnpg_lines += f"\n    options={type(cnpg.options).__name__}({opt_args}),"
        if cnpg.database is not None:
            db_fields = dump(cnpg.database)
            db_args = ", ".join(f"{k}={v!r}" for k, v in db_fields.items())
            cnpg_lines += f"\n    database=DatabaseConfig({db_args}),"
        body += [
            "",
            "database = stack.build(",
            "    Database,",
            cnpg_lines,
            ")",
            "database_backend = DatabaseBackend()",
            "database_backend.create(database)",
        ]
        if cnpg.database is not None:
            body.append("database_backend.create_cluster(database)")
        body.append("stack.record(database)")

    # Control planes come straight after their Postgres and Redis, ahead of
    # the gateway: it verifies every API key against the organization one.
    for controlplane in controlplanes:
        imports.append("from multistack import ControlPlane, ControlPlaneBackend")
        # The admin and organization control planes are two instances of
        # one capability and the real platform runs both, calling each
        # other as peers -- so they need distinct variable names.
        cp_var = "control_plane" if len(controlplanes) == 1 else f"control_plane_{controlplane.type}"
        # Only kubeconfig_path is a placeholder -- existing_secret is a
        # real value like Gateway.api_key_secret, never invented here
        # (see mcp_tools/controlplane.py's SECURITY note). database_url/
        # cache_url are NEVER wired in either: ControlPlane.FROM_STACK
        # doesn't declare them at all (see ControlPlane's own module
        # docstring -- they're deliberately credential-free, so they
        # cannot serve as the real DATABASE_URL/REDIS_URL the Secret
        # named by existing_secret must already hold).
        # ControlPlane -> billing. Unset, both control plane types fall back
        # to http://billing:8000, which resolves nowhere on a real cluster.
        # Billing is built further down, so this is its own endpoint, the
        # same way the policy's admin_cp_url is wired above.
        wire_billing = billing is not None and controlplane.billing_internal_url is None
        cp_exclude = frozenset(ControlPlane.FROM_STACK) | {"options"}
        if wire_billing:
            cp_exclude |= {"billing_internal_url"}
        controlplane_lines = _field_lines(controlplane, exclude=cp_exclude)
        if wire_billing:
            controlplane_lines += f"\n    billing_internal_url={billing.endpoint!r},"
        if controlplane.options is not None:
            options_cls = type(controlplane.options).__name__
            imports.append(f"from multistack.controlplane import {options_cls}")
            opt_args = ", ".join(f"{k}={v!r}" for k, v in dump(controlplane.options).items())
            controlplane_lines += f"\n    options={options_cls}({opt_args}),"
        body += [
            "",
            f"{cp_var} = stack.build(",
            "    ControlPlane,",
            controlplane_lines,
            ")",
            "controlplane_backend = ControlPlaneBackend()",
            f"controlplane_backend.create({cp_var})",
            f"stack.record({cp_var})",
        ]

    if observability is not None:
        imports.append("from multistack import Observability, ObservabilityBackend")
        # kubeconfig_path AND storage_class are both placeholders here --
        # Observability.FROM_STACK declares both, and this tool only
        # allows composing observability alongside a composed `storage`
        # layer (see build_full_stack_plan's own dependency check), so
        # both entries are always placeholders whenever this block
        # renders at all -- same reasoning as MinIO's storage_class.
        observability_lines = _field_lines(
            observability, exclude=frozenset(Observability.FROM_STACK) | {"options"}
        )
        if observability.options is not None:
            imports.append("from multistack.observability import KubePrometheusStackOptions")
            opt_args = ", ".join(f"{k}={v!r}" for k, v in dump(observability.options).items())
            observability_lines += f"\n    options=KubePrometheusStackOptions({opt_args}),"
        body += [
            "",
            "observability = stack.build(",
            "    Observability,",
            observability_lines,
            ")",
            "observability_backend = ObservabilityBackend()",
            "observability_backend.create(observability)",
            "stack.record(observability)",
        ]

    if queue is not None:
        imports.append("from multistack import Queue, QueueBackend")
        if queue.options is not None:
            imports.append("from multistack import NatsQueueOptions")
        # kubeconfig_path and storage_class come from the Stack. Recording
        # the queue publishes event_backbone_url, which Stack fills into the
        # gateway and policies; the enricher and billing read queue.endpoint.
        # create() also makes both streams, gateway.events and
        # gateway.events.enriched.
        queue_lines = _field_lines(queue, exclude=frozenset(Queue.FROM_STACK) | {"options"})
        if queue.options is not None:
            opt_args = ", ".join(f"{k}={v!r}" for k, v in dump(queue.options).items())
            queue_lines += f"\n    options=NatsQueueOptions({opt_args}),"
        body += [
            "",
            "queue = stack.build(",
            "    Queue,",
            queue_lines,
            ")",
            "queue_backend = QueueBackend()",
            "queue_backend.create(queue)",
            "stack.record(queue)",
        ]

    if tokenizer is not None:
        imports.append("from multistack import Tokenizer, TokenizerBackend")
        # Only kubeconfig_path is a placeholder -- Tokenizer has no other
        # FROM_STACK entry. Its consumer is the enricher, which reads the
        # tokenizer_url this record publishes (see the enricher block).
        tokenizer_lines = _field_lines(tokenizer, exclude=frozenset(Tokenizer.FROM_STACK) | {"options"})
        if tokenizer.options is not None:
            imports.append("from multistack.tokenizer import TiktokenOptions")
            opt_args = ", ".join(f"{k}={v!r}" for k, v in dump(tokenizer.options).items())
            tokenizer_lines += f"\n    options=TiktokenOptions({opt_args}),"
        body += [
            "",
            "tokenizer = stack.build(",
            "    Tokenizer,",
            tokenizer_lines,
            ")",
            "tokenizer_backend = TokenizerBackend()",
            "tokenizer_backend.create(tokenizer)",
            "stack.record(tokenizer)",
        ]

    if enricher is not None:
        imports.append("from multistack import Enricher, EnricherBackend")
        # The tokenizer wires in HERE now, not into a tpm policy: ADR-030
        # made the enricher the thing that guarantees a token count, and
        # TPMOptions.tokenizer_url was deleted. tokenizer_url lives on the
        # options object, so Stack.build()'s FROM_STACK fill (top-level
        # fields only) cannot reach it -- stack.get() does it explicitly,
        # exactly as the policy block used to.
        enricher_exclude = frozenset(Enricher.FROM_STACK) | {"options"}
        if queue is not None:
            enricher_exclude |= {"event_backbone_url"}
        enricher_lines = _field_lines(enricher, exclude=enricher_exclude)
        if queue is not None:
            enricher_lines += "\n    event_backbone_url=queue.endpoint,"
        if enricher.options is not None:
            imports.append("from multistack.enricher import EnricherOptions")
            wire_tokenizer = tokenizer is not None
            opt_exclude = {"tokenizer_url"} if wire_tokenizer else set()
            opt_args = ", ".join(
                f"{k}={v!r}" for k, v in dump(enricher.options).items() if k not in opt_exclude
            )
            if wire_tokenizer:
                opt_args += (", " if opt_args else "") + "tokenizer_url=stack.get('tokenizer_url')"
            enricher_lines += f"\n    options=EnricherOptions({opt_args}),"
        body += [
            "",
            "enricher = stack.build(",
            "    Enricher,",
            enricher_lines,
            ")",
            "enricher_backend = EnricherBackend()",
            "enricher_backend.create(enricher)",
        ]

    # Policy is rendered BEFORE Gateway (not the arbitrary order this once
    # was) specifically so the gateway block below can reference an
    # already-built policy_rpm/policy_tpm variable directly -- the request
    # path's Gateway -> Rate Limiter arrows can't be wired any other way,
    # since Gateway.policy_endpoints has no FROM_STACK entry for
    # Stack.build() to fill it from, and with two limiters composed
    # there's no single Stack-tracked "the" policy endpoint to read back
    # (Stack holds whichever of several same-capability instances was
    # recorded last -- see the controlplane/portal blocks below for the
    # same limitation).
    policy_vars: list[str] = []
    admin_cp = next((cp for cp in controlplanes if cp.type == "admin"), None)
    for policy in policies:
        imports.append("from multistack import Policy, PolicyBackend, RateLimits")
        # Distinct variable names when more than one limiter is composed.
        # The real platform runs rpm and tpm side by side -- both consume
        # the gateway's events and both keep their own counters -- so a
        # single `policy` variable would have the second overwrite the
        # first in the generated script.
        var = "policy" if len(policies) == 1 else f"policy_{policy.type}"
        # kubeconfig_path is always a placeholder. cache_url joins it,
        # but only when valkey is also composed -- Cache.PROVIDES a
        # real cache_url now, so Stack fills it in from the recorded
        # cache layer. event_backbone_url is a real value nothing in
        # this script produces, so it always renders like any other
        # explicit field (see the module docstring's SCOPE NOTE).
        policy_exclude = {"kubeconfig_path", "options", "limits"}
        if valkey is not None:
            policy_exclude.add("cache_url")
        if queue is not None:
            policy_exclude.add("event_backbone_url")
        # Policy -> admin control plane, for per-org plan limits. Not in
        # Policy.FROM_STACK, and the control plane is built further down,
        # so the URL is the composed spec's own endpoint, which depends
        # only on its type, namespace and port. Wired only when the caller
        # gave the service key: the URL without it 401s and falls back to
        # the static limits with no error.
        wire_admin_cp = (
            admin_cp is not None
            and not policy.admin_cp_url
            and policy.admin_cp_service_api_key is not None
        )
        if wire_admin_cp:
            policy_exclude.add("admin_cp_url")
        policy_lines = _field_lines(policy, exclude=policy_exclude)
        if wire_admin_cp:
            policy_lines += f"\n    admin_cp_url={admin_cp.endpoint!r},"
        limits_args = ", ".join(f"{k}={v!r}" for k, v in dump(policy.limits).items())
        policy_lines += f"\n    limits=RateLimits({limits_args}),"
        if policy.options is not None:
            # Import whichever options class this policy actually uses --
            # RPMOptions and TPMOptions are both real now (Policy supports
            # type="tpm"); hardcoding RPMOptions produced a script that
            # referenced a class it never imported for a tpm policy.
            options_cls = type(policy.options).__name__
            imports.append(f"from multistack.policy import {options_cls}")
            # No tokenizer wiring here any more. ADR-030 moved token
            # counting into the Enricher, which guarantees a count and
            # republishes to gateway.events.enriched; the tpm limiter now
            # consumes that enriched stream and never calls a tokenizer,
            # so TPMOptions.tokenizer_url was DELETED from the SDK, not
            # deprecated. Rendering it raises extra_forbidden.
            opt_args = ", ".join(f"{k}={v!r}" for k, v in dump(policy.options).items())
            policy_lines += f"\n    options={options_cls}({opt_args}),"
        body += [
            "",
            f"{var} = stack.build(",
            "    Policy,",
            policy_lines,
            ")",
            "policy_backend = PolicyBackend()",
            f"{var}_endpoint = policy_backend.create({var})",
            f"stack.record({var})",
        ]
        policy_vars.append(var)

    if gateway is not None:
        imports.append("from multistack import Gateway, GatewayBackend")
        # upstream_url and policy_endpoints are the request path's own
        # Gateway -> Inference and Gateway -> Rate Limiter arrows.
        # Neither has a FROM_STACK entry on Gateway, so Stack.build()
        # cannot fill either one automatically the way kubeconfig_path
        # is -- this tool wires them explicitly instead, the same
        # reasoning as the tokenizer -> enricher wiring above.
        gateway_exclude = {"kubeconfig_path", "options"}
        if queue is not None:
            gateway_exclude.add("event_backbone_url")
        if org_cp is not None:
            gateway_exclude.add("org_cp_internal_url")
        wire_upstream = inference is not None
        if wire_upstream:
            gateway_exclude.add("upstream_url")
        # "Explicit beats wired": only fill policy_endpoints when the
        # caller left it at its [] default -- a caller who already named
        # real endpoints (attaching to a policy deployed outside this
        # call) is not missing anything, and must not be overridden.
        wire_policy_endpoints = bool(policy_vars) and not gateway.policy_endpoints
        if wire_policy_endpoints:
            gateway_exclude.add("policy_endpoints")
        gateway_lines = _field_lines(gateway, exclude=gateway_exclude)
        if org_cp is not None:
            gateway_lines += f"\n    org_cp_internal_url={org_cp.endpoint!r},"
        if wire_upstream:
            gateway_lines += "\n    upstream_url=stack.get('inference_endpoint'),"
        if wire_policy_endpoints:
            endpoints = ", ".join(f"{v}.endpoint" for v in policy_vars)
            gateway_lines += f"\n    policy_endpoints=[{endpoints}],"
        if gateway.options is not None:
            imports.append("from multistack.gateway import ModelGatewayOptions")
            opt_args = ", ".join(f"{k}={v!r}" for k, v in dump(gateway.options).items())
            gateway_lines += f"\n    options={type(gateway.options).__name__}({opt_args}),"
        body += [
            "",
            "gateway = stack.build(",
            "    Gateway,",
            gateway_lines,
            ")",
            "gateway_backend = GatewayBackend()",
            "gateway_endpoint = gateway_backend.create(gateway)",
            "stack.record(gateway)",
        ]

    if ingress is not None:
        # IngressGateway/IngressGatewayBackend are NOT re-exported at the
        # top-level multistack package -- import from the submodule, same
        # as mcp_tools/ingress_gateway.py does standalone.
        imports.append("from multistack.ingress_gateway import IngressGateway, IngressGatewayBackend")
        # Only kubeconfig_path is a placeholder here -- address_pool has
        # no FROM_STACK entry at all, it's always a real value.
        ingress_lines = _field_lines(ingress, exclude={"kubeconfig_path", "options"})
        if ingress.options is not None:
            imports.append("from multistack.ingress_gateway import MetalLBIstioOptions")
            opt_args = ", ".join(f"{k}={v!r}" for k, v in dump(ingress.options).items())
            ingress_lines += f"\n    options={type(ingress.options).__name__}({opt_args}),"
        body += [
            "",
            "ingress = stack.build(",
            "    IngressGateway,",
            ingress_lines,
            ")",
            "ingress_backend = IngressGatewayBackend()",
            "ingress_external_endpoint = ingress_backend.create(ingress)",
            "stack.record(ingress)",
        ]

    for portal in portals:
        imports.append("from multistack import Portal, PortalBackend")
        portal_var = "portal" if len(portals) == 1 else f"portal_{portal.type}"
        # kubeconfig_path is always a placeholder. api_upstream joins it
        # only when controlplane is also composed -- Portal.FROM_STACK
        # maps api_upstream <- controlplane_endpoint, so Stack fills it
        # in for real from the ControlPlane recorded just above (this
        # tool's own validation already rejected a portal/controlplane
        # type mismatch, so the wiring always lands on the right
        # control plane). Without a composed controlplane, api_upstream
        # is a real field like any other -- build_full_stack_plan's own
        # validation already confirmed portal.validate() passed, so
        # either it's non-empty or allow_no_api was explicitly set.
        portal_exclude = {"kubeconfig_path", "options"}
        if len(controlplanes) == 1:
            portal_exclude.add("api_upstream")
        # With BOTH control planes composed, Stack's controlplane_endpoint
        # holds whichever was recorded last, so it cannot be trusted to
        # match this portal -- the SDK says as much in multistack/stack.py.
        # Each portal then carries its own real api_upstream instead, which
        # build_full_stack_plan has already required.
        # Portal -> gateway, for the organization portal's chat. Unset, /mg
        # falls through to the SPA and returns index.html with a 200, and
        # chat shows "Unable to load models". The admin portal has no chat,
        # so it is never wired.
        wire_gateway = gateway is not None and portal.type == "organization" and not portal.gateway_upstream
        if wire_gateway:
            portal_exclude.add("gateway_upstream")
        portal_lines = _field_lines(portal, exclude=portal_exclude)
        if wire_gateway:
            portal_lines += "\n    gateway_upstream=gateway.endpoint,"
        if portal.options is not None:
            options_cls = type(portal.options).__name__
            imports.append(f"from multistack.portal import {options_cls}")
            opt_args = ", ".join(f"{k}={v!r}" for k, v in dump(portal.options).items())
            portal_lines += f"\n    options={options_cls}({opt_args}),"
        body += [
            "",
            f"{portal_var} = stack.build(",
            "    Portal,",
            portal_lines,
            ")",
            "portal_backend = PortalBackend()",
            f"portal_backend.create({portal_var})",
            f"stack.record({portal_var})",
        ]

    if billing is not None:
        imports.append("from multistack import Billing, BillingBackend")
        # existing_secret is a REAL value like ControlPlane's -- this tool
        # builds no Secret. Only kubeconfig_path is a placeholder.
        billing_exclude = frozenset(Billing.FROM_STACK) | {"options"}
        if queue is not None:
            billing_exclude |= {"event_backbone_url"}
        billing_lines = _field_lines(billing, exclude=billing_exclude)
        if queue is not None:
            billing_lines += "\n    event_backbone_url=queue.endpoint,"
        if billing.options is not None:
            options_cls = type(billing.options).__name__
            imports.append(f"from multistack.billing import {options_cls}")
            opt_args = ", ".join(f"{k}={v!r}" for k, v in dump(billing.options).items())
            billing_lines += f"\n    options={options_cls}({opt_args}),"
        body += [
            "",
            "billing = stack.build(",
            "    Billing,",
            billing_lines,
            ")",
            "billing_backend = BillingBackend()",
            "billing_backend.create(billing)",
            "stack.record(billing)",
        ]

    for route in routes:
        imports.append("from multistack import Route, RouteBackend")
        # A route is per-service and there are usually several, so each
        # gets its own variable named after the route itself.
        route_var = "route" if len(routes) == 1 else f"route_{route.name.replace('-', '_')}"
        # ingress_address joins kubeconfig_path as a placeholder whenever
        # `ingress` is composed: Route.FROM_STACK maps it from the
        # ingress_gateway's own endpoint, which MetalLB only assigns once
        # the script runs.
        route_exclude = {"kubeconfig_path", "options"}
        if ingress is not None:
            route_exclude.add("ingress_address")
        route_lines = _field_lines(route, exclude=route_exclude)
        if route.options is not None:
            options_cls = type(route.options).__name__
            imports.append(f"from multistack.route import {options_cls}")
            opt_args = ", ".join(f"{k}={v!r}" for k, v in dump(route.options).items())
            route_lines += f"\n    options={options_cls}({opt_args}),"
        body += [
            "",
            f"{route_var} = stack.build(",
            "    Route,",
            route_lines,
            ")",
            "route_backend = RouteBackend()",
            f"route_backend.create({route_var})",
        ]

    script = "\n".join(imports) + "\n\n" + "\n".join(body) + "\n"
    check_script(script)
    return script


@mcp.tool()
@validate_call
def build_full_stack_plan(
    cluster: Optional[RKE2Cluster] = None,
    kubeconfig_path: Optional[str] = None,
    existing_storage_class: Optional[str] = None,
    storage: Optional[Storage] = None,
    minio: Optional[MinIOTenant] = None,
    inference: Optional[Inference] = None,
    valkey: Optional[Cache] = None,
    cnpg: Optional[Database] = None,
    observability: Optional[Observability] = None,
    queue: Optional[Queue] = None,
    tokenizer: Optional[Tokenizer] = None,
    enricher: Optional[Enricher] = None,
    accelerator: Optional[Accelerator] = None,
    gateway: Optional[Gateway] = None,
    policy: Union[Policy, List[Policy], None] = None,
    ingress: Optional[IngressGateway] = None,
    controlplane: Union[ControlPlane, List[ControlPlane], None] = None,
    portal: Union[Portal, List[Portal], None] = None,
    billing: Optional[Billing] = None,
    route: Union[Route, List[Route], None] = None,
) -> dict:
    """
    Validate and render ONE script that deploys a Multistack platform, or
    any part of it, together with every component that part depends on.
    Does NOT provision anything -- read-only, safe to call freely.

    Use this for any component that depends on others -- gateway, policy,
    enricher, tokenizer, billing, controlplane, portal, queue, minio,
    observability, route. The plan must include the whole dependency
    chain below, never the requested component alone.

    Target cluster -- give exactly one:
    - `cluster`: a new RKE2 cluster, built first.
    - `kubeconfig_path`: an EXISTING cluster; the user's real path. For
      queue/minio/observability, ask which StorageClass it already has and
      pass it as `existing_storage_class` -- never ask for node addresses,
      `storage` can only be composed onto a new cluster.

    Dependencies -- a plan missing one is rejected, naming what to add:
    - `gateway` needs an organization-type `controlplane`, which needs
      `cnpg` (with `cnpg.database`) and `valkey`. This is the baseline for
      every request: the gateway verifies each API key against it. It also
      needs an inference upstream: compose `inference`, or set
      `gateway.upstream_url` to the user's real external endpoint.
    - `policy` needs `gateway`, `valkey` and `queue`; a type="tpm" policy
      also needs `enricher` (it reads gateway.events.enriched).
    - `enricher` needs `gateway`, `queue` and `tokenizer` (its fallback);
      `tokenizer` needs `enricher` (its only caller).
    - `billing` needs `cnpg` (with `cnpg.database`), `enricher` and `queue`.
    - each `portal` needs a `controlplane` of the same type.
    - `queue`, `minio` and `observability` need storage: `storage`, or
      `existing_storage_class` on an existing cluster.
    - `route` needs `ingress`.
    When a dependency needs a real value you don't have, ask the user for
    it -- never drop the dependency. Ask only for fields that are required,
    have no default, and are neither wired nor placeholders below; leave
    every other field at its default rather than asking about it.

    Wired for you, from the composed component, in the generated script:
    queue -> `event_backbone_url` on gateway, policy, enricher and billing;
    valkey -> `policy.cache_url`; organization controlplane ->
    `gateway.org_cp_internal_url`; inference -> `gateway.upstream_url`;
    every policy -> `gateway.policy_endpoints`; tokenizer ->
    `enricher.options.tokenizer_url`; minio -> `inference.s3_endpoint_url`
    (s3:// model only); controlplane -> `portal.api_upstream` (one
    controlplane only; with both, give each portal its real upstream);
    ingress -> `route.ingress_address`; billing ->
    `controlplane.billing_internal_url`; gateway -> an organization
    portal's `gateway_upstream`; admin controlplane -> `policy.admin_cp_url`
    (only when `admin_cp_service_api_key` is given).
    A wired field that the SDK requires at construction is a PLACEHOLDER:
    pass any non-empty string and it is discarded. The same goes for every
    component's `kubeconfig_path` and for `storage_class` on minio,
    observability and queue. For `event_backbone_url` with `queue`
    composed, pass the queue's default, nats://nats.platform.svc.cluster.local:4222.
    Optional wired fields (`policy_endpoints`, `admin_cp_url`,
    `billing_internal_url`, `gateway_upstream`) keep an explicit value.

    Never placeholders -- real values only, ask the user:
    `gateway.upstream_url` unless `inference` is composed (the model
    server's real URL, or compose `inference`), `gateway.api_key_secret`, `controlplane.existing_secret`,
    `billing.existing_secret`, `cnpg.database` (its password is real
    plaintext -- never invent one), `policy.cache_auth_url` (the cache URL
    with its password; the chart enables auth by default, and a limiter
    without it fails open), `policy.admin_cp_service_api_key`,
    `inference.s3_secret_name`, `ingress.address_pool`.

    The script builds components in dependency order whatever order the
    arguments came in.

    Returns {"valid": True, ..., "script": <the deployable script>} on
    success, or {"valid": False, "error": <reason>} if a dependency is
    missing or any component's own validation rejected it.
    """
    # One instance or several: the real platform runs rpm AND tpm, both
    # control planes, and both portals (see multistack/stack.py, which
    # calls them "two instances of one capability"). Accepting either
    # shape keeps every single-instance caller working unchanged.
    def _as_list(value):
        if value is None:
            return []
        return list(value) if isinstance(value, list) else [value]

    policies = _as_list(policy)
    controlplanes = _as_list(controlplane)
    portals = _as_list(portal)
    routes = _as_list(route)

    if (cluster is None) == (kubeconfig_path is None):
        return {
            "valid": False,
            "error": (
                "give exactly one of `cluster` (a new RKE2 cluster, built first) or "
                "`kubeconfig_path` (the real path to an existing cluster's kubeconfig)"
            ),
        }
    if cluster is not None and existing_storage_class:
        return {
            "valid": False,
            "error": "existing_storage_class is for an existing cluster (kubeconfig_path) -- a new cluster has none; compose `storage` instead",
        }
    if cluster is None and storage is not None:
        return {
            "valid": False,
            "error": (
                "`storage` can't be composed onto an existing cluster here -- installing "
                "Longhorn needs the cluster's node list. If the cluster already has a "
                "StorageClass, pass its name as `existing_storage_class`; otherwise build "
                "storage first with build_storage_plan"
            ),
        }
    has_storage = storage is not None or bool(existing_storage_class)

    route_names = [r.name for r in routes]
    if len(set(route_names)) != len(route_names):
        return {
            "valid": False,
            "error": f"two route entries share the same name ({route_names}) -- each route needs a distinct name.",
        }

    for label, items in (("policy", policies), ("controlplane", controlplanes), ("portal", portals)):
        kinds = [i.type for i in items]
        if len(set(kinds)) != len(kinds):
            return {
                "valid": False,
                "error": (
                    f"two {label} entries share the same type ({kinds}) -- they would "
                    "collide on the same release name and namespace. Each instance of a "
                    "capability needs a distinct type."
                ),
            }

    if billing is not None and (cnpg is None or cnpg.database is None):
        return {
            "valid": False,
            "error": (
                "billing requires a real database (cnpg, with cnpg.database set) -- "
                "it has its own Postgres schema and migration Job. Pass one, or drop billing"
            ),
        }

    if billing is not None and enricher is None:
        return {
            "valid": False,
            "error": (
                "billing requires an enricher -- its consumer drains the "
                "enricher's derived gateway.events.enriched stream, and nothing "
                "else produces it. Without one, billing is deployed, healthy, "
                "and meters nothing. Pass enricher too, or drop billing"
            ),
        }

    # The gateway's request path has one baseline dependency nothing else
    # in this tool substitutes for: ADR-026 removed static-key auth, so
    # every /v1 call is verified synchronously against the ORGANIZATION
    # control plane (org_cp_internal_url) -- "the gateway won't accept a
    # request without it," not a soft recommendation. Requiring the
    # control plane here cascades correctly through the controlplane
    # dependency check above: composing it already forces a real database
    # and cache to exist too, which is the platform's own documented
    # baseline (Postgres + Redis + Organization Control Plane + Gateway)
    # satisfied by one rule instead of four separate ones. The admin
    # control plane is NOT required here -- it owns plans/permissions,
    # independent of the request path.
    if gateway is not None and not any(cp.type == "organization" for cp in controlplanes):
        return {
            "valid": False,
            "error": (
                "gateway requires an organization control plane composed -- "
                "every request is verified synchronously against it "
                "(org_cp_internal_url), so a gateway built without one is "
                "deployed, healthy, and rejects every real request with "
                "\"key verification is unavailable\". Pass an organization "
                "controlplane too (which itself requires a database and a "
                "cache -- see that dependency check), or drop gateway"
            ),
        }

    # TPMOptions.event_stream_subject is a REAL, driver-honored override
    # (the tpm chart's streamSubject config comes straight from it -- see
    # multistack/policy/drivers/tpm.py), so a caller who has deliberately
    # pointed it somewhere other than the enricher's derived stream is not
    # missing anything -- only the untouched default is.
    tpm_needs_enricher = [
        p for p in policies
        if p.type == "tpm"
        and (p.options is None or getattr(p.options, "event_stream_subject", None) == "gateway.events.enriched")
    ]
    if tpm_needs_enricher and enricher is None:
        return {
            "valid": False,
            "error": (
                "a type=\"tpm\" policy requires an enricher -- by default it "
                "reads gateway.events.enriched (TPMOptions.event_stream_subject), "
                "which only the enricher publishes; it does not compute token "
                "counts itself. Without one, the tpm limiter is deployed, "
                "healthy, and counts nothing, silently allowing every request. "
                "Pass enricher too, or drop the tpm policy"
            ),
        }

    for one_policy in policies:
        error = admin_cp_key_error(one_policy)
        if error:
            return {"valid": False, "error": f"policy type={one_policy.type!r}: {error}"}

    if routes and ingress is None:
        return {
            "valid": False,
            "error": (
                "route requires an ingress gateway -- a route goes THROUGH the front "
                "door, it does not create one. Pass ingress too, or drop route"
            ),
        }

    for one_route in routes:
        # Route has no model_validator re-running its own validate() at
        # construction, the same gap Portal has.
        try:
            one_route.validate()
        except ValueError as e:
            return {"valid": False, "error": str(e)}

    if minio is not None and not has_storage:
        return {"valid": False, "error": "minio requires storage -- pass storage too (or existing_storage_class on an existing cluster), or drop minio"}

    if observability is not None and not has_storage:
        return {"valid": False, "error": "observability requires storage -- pass storage too (or existing_storage_class on an existing cluster), or drop observability"}

    if cnpg is not None and cnpg.database is not None:
        # Constructing a Database checks the operator half only -- the
        # cluster half is optional, because the same spec drives an
        # operator-only install. So a `database` block given without
        # `cnpg.name` still constructs (see mcp_tools/cnpg.py's own
        # build_cnpg_plan for the same check).
        try:
            cnpg.validate_cluster()
        except ValueError as e:
            return {"valid": False, "error": str(e)}

    if controlplanes and (cnpg is None or cnpg.database is None or valkey is None):
        return {
            "valid": False,
            "error": (
                "controlplane requires both a real database (cnpg, with "
                "cnpg.database set) and a cache (valkey) -- pass both, or "
                "drop controlplane"
            ),
        }

    if len(portals) == 1 and len(controlplanes) == 1 and portals[0].type != controlplanes[0].type:
        return {
            "valid": False,
            "error": (
                f"portal.type={portals[0].type!r} does not match "
                f"controlplane.type={controlplanes[0].type!r} -- a composed "
                "portal auto-wires its api_upstream from the composed "
                "controlplane's endpoint, so mixing an admin portal with "
                "an organization control plane (or vice versa) would "
                "silently wire the wrong upstream in. Match the two "
                "types, or build the mismatched pair in separate calls."
            ),
        }

    for one_portal in portals:
        # Portal has no model_validator re-running its own validate() at
        # construction (unlike Gateway/Policy) -- see
        # mcp_tools/portal.py's own build_portal_plan for the same check.
        try:
            one_portal.validate()
        except ValueError as e:
            return {"valid": False, "error": str(e)}
        # With several control planes composed, Stack's controlplane_endpoint
        # is whichever was recorded last, so it cannot be relied on to be
        # this portal's. Each portal must then name its own upstream.
        if len(controlplanes) > 1 and not one_portal.api_upstream:
            return {
                "valid": False,
                "error": (
                    f"portal type={one_portal.type!r} needs an explicit api_upstream: with "
                    "more than one control plane composed, the Stack records only the last "
                    "one, so the right upstream cannot be inferred. Pass the matching "
                    "control plane's endpoint."
                ),
            }

    for one_portal in portals:
        if not any(cp.type == one_portal.type for cp in controlplanes):
            return {
                "valid": False,
                "error": (
                    f"portal type={one_portal.type!r} needs a {one_portal.type!r} controlplane "
                    "in this plan -- it is the portal's API. Add controlplane with "
                    f"type={one_portal.type!r} (which itself needs cnpg and valkey)"
                ),
            }

    error = _missing_dependency(
        {
            "storage": has_storage, "minio": minio, "observability": observability,
            "queue": queue, "accelerator": accelerator, "inference": inference,
            "valkey": valkey, "cnpg": cnpg, "controlplane": controlplanes,
            "portal": portals, "gateway": gateway, "policy": policies,
            "enricher": enricher, "tokenizer": tokenizer, "billing": billing,
            "ingress": ingress, "route": routes,
        },
        opted_out_of_backbone={
            "policy": bool(policies) and all(p.allow_no_backbone for p in policies),
            "enricher": enricher is not None and enricher.allow_no_backbone,
            "billing": billing is not None and billing.allow_no_backbone,
        },
    )
    if error:
        return {"valid": False, "error": error}

    try:
        script = _render_script(
            cluster, kubeconfig_path, existing_storage_class, storage, minio, inference,
            valkey, cnpg, observability, queue, tokenizer, enricher, accelerator, gateway,
            policies, ingress, controlplanes, portals, billing, routes,
        )
    except AssertionError as e:
        return {"valid": False, "error": str(e)}

    layers = (
        (["rke2"] if cluster is not None else [])
        + (["storage"] if storage else [])
        + (["minio"] if minio else [])
        # accelerator is listed here, ahead of inference, to match the
        # order the script actually builds them in -- it has to exist
        # before inference does, or a GPU pod is created before
        # nvidia.com/gpu is a resource anything can request.
        + (["accelerator"] if accelerator else [])
        + (["inference"] if inference else [])
        + (["valkey"] if valkey else [])
        + (["cnpg"] if cnpg else [])
        # Control planes follow their Postgres and Redis, ahead of the
        # gateway that verifies keys against the organization one.
        + _layer_names("controlplane", controlplanes)
        + (["observability"] if observability else [])
        + (["queue"] if queue else [])
        + (["tokenizer"] if tokenizer else [])
        + (["enricher"] if enricher else [])
        # policy listed here, ahead of gateway, to match the order the
        # script actually builds them in -- a composed gateway's
        # policy_endpoints (and upstream_url, from inference above) are
        # wired from these, so they have to exist first.
        #
        # One entry per instance. A lone instance keeps its plain name;
        # several are suffixed by type, so composing rpm AND tpm (or both
        # control planes, or both portals) is visible in the reply rather
        # than collapsing to one name.
        + _layer_names("policy", policies)
        + (["gateway"] if gateway else [])
        + (["ingress"] if ingress else [])
        + _layer_names("portal", portals)
        + (["billing"] if billing else [])
        + [f"route:{r.name}" for r in routes]
    )
    result = {"valid": True, "layers": layers, "script": script}
    if cluster is not None:
        result["cluster_name"] = cluster.name
    else:
        result["kubeconfig_path"] = kubeconfig_path
    # inference.endpoint/valkey.endpoint/cnpg.endpoint/gateway.endpoint/
    # policy.endpoint are deterministic properties (a string formula over
    # name/release_name/namespace/port, same as the standalone
    # build_inference_plan/build_valkey_plan/build_cnpg_plan/
    # build_gateway_plan/build_policy_plan already return) -- computable
    # here with no real backend call. Without these, the model has no
    # real value to quote for "what will the endpoint be" and has been
    # observed inventing a wrong one instead of leaving it unstated.
    #
    # ingress.external_endpoint has no such formula -- MetalLB only
    # assigns a real address once the script actually runs, so there is
    # deliberately no ingress_endpoint here to invent one from. cnpg's
    # endpoint is None unless database was given (see Database.endpoint),
    # which is exactly when database_url is worth returning at all.
    if inference is not None:
        result["inference_endpoint"] = inference.endpoint
    if valkey is not None:
        result["valkey_endpoint"] = valkey.endpoint
    if cnpg is not None and cnpg.database is not None:
        result["database_url"] = cnpg.endpoint
    if observability is not None:
        result["observability_endpoint"] = observability.grafana_endpoint()
    if queue is not None:
        result["event_backbone_url"] = queue.endpoint
    if tokenizer is not None:
        result["tokenizer_url"] = tokenizer.endpoint
    if gateway is not None:
        result["gateway_endpoint"] = gateway.endpoint
    # Keyed by type when several are composed, so a reply naming an
    # endpoint says which instance it belongs to. The single-instance
    # keys stay exactly as they were.
    if len(policies) == 1:
        result["policy_endpoint"] = policies[0].endpoint
    elif policies:
        result["policy_endpoints"] = {pol.type: pol.endpoint for pol in policies}
    if len(controlplanes) == 1:
        result["controlplane_endpoint"] = controlplanes[0].endpoint
    elif controlplanes:
        result["controlplane_endpoints"] = {cp.type: cp.endpoint for cp in controlplanes}
    if billing is not None:
        result["billing_endpoint"] = billing.endpoint
    if routes:
        # Route.url falls back to a literal <ingress-address> placeholder
        # until MetalLB has actually assigned one, so these are only real
        # when the caller gave a hostname.
        result["route_urls"] = {r.name: r.url for r in routes}
    if len(portals) == 1:
        result["portal_endpoint"] = portals[0].endpoint
    elif portals:
        result["portal_endpoints"] = {prt.type: prt.endpoint for prt in portals}
    return result
