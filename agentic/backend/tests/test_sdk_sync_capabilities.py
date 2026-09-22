"""Guards every capability tool + SKILL.md against silently falling out
of sync with the real SDK -- the same job tests/test_sdk_sync_rke2.py
does for RKE2, generalized to all of them.

This exists because the drift it checks for actually happened: the SDK
grew NetworkPolicy support (Gateway/Policy/MinIOTenant's
`network_policy_allowed_ingress`, Inference's `allowed_client_labels`)
and IngressGateway's `node_selector`, and every one of those fields was
absent from the matching SKILL.md for as long as it took someone to
notice. An undocumented field isn't cosmetic here: SKILL.md is the only
description of a field the model ever reads, so a field missing from it
is a field the model can neither offer nor use correctly.

Adding a capability means adding one row to CAPABILITIES below -- these
tests then cover it automatically.
"""

from __future__ import annotations

import ast
import re
import typing
from pathlib import Path

import pytest

from multistack import (
    Billing,
    Enricher,
    Route,
    ControlPlane,
    Database,
    Gateway,
    Inference,
    MinIOTenant,
    Observability,
    Policy,
    Portal,
    Queue,
    RKE2Cluster,
    Storage,
    Tokenizer,
    Cache,
)
from multistack.accelerator import Accelerator
from multistack.ingress_gateway import IngressGateway

from mcp_tools import accelerator as mcp_accelerator
from mcp_tools import billing as mcp_billing
from mcp_tools import cnpg as mcp_cnpg
from mcp_tools import enricher as mcp_enricher
from mcp_tools import route as mcp_route
from mcp_tools import controlplane as mcp_controlplane
from mcp_tools import full_stack as mcp_full_stack
from mcp_tools import gateway as mcp_gateway
from mcp_tools import inference as mcp_inference
from mcp_tools import ingress_gateway as mcp_ingress_gateway
from mcp_tools import minio as mcp_minio
from mcp_tools import observability as mcp_observability
from mcp_tools import policy as mcp_policy
from mcp_tools import portal as mcp_portal
from mcp_tools import queue as mcp_queue
from mcp_tools import rke2 as mcp_rke2
from mcp_tools import storage as mcp_storage
from mcp_tools import tokenizer as mcp_tokenizer
from mcp_tools import valkey as mcp_valkey
from orchestration.tools import ALL_TOOLS

SKILLS_DIR = Path(__file__).resolve().parent.parent / "src" / "skills"

KC = "/tmp/kc.yaml"
NODES = [{"address": "10.0.0.11", "role": "server"}]

# (id, tool function, the spec argument's name, the real spec class,
#  skills/<dir>, minimal kwargs that build a valid plan)
CAPABILITIES = [
    ("rke2", mcp_rke2.build_rke2_cluster_plan, "cluster", RKE2Cluster, "rke2-cluster",
     {"name": "c", "nodes": NODES}),
    ("storage", mcp_storage.build_storage_plan, "storage", Storage, "storage",
     {"type": "longhorn", "kubeconfig_path": KC}),
    ("minio", mcp_minio.build_minio_plan, "tenant", MinIOTenant, "minio",
     {"kubeconfig_path": KC, "name": "t", "servers": 2, "volumes_per_server": 2}),
    ("inference", mcp_inference.build_inference_plan, "inference", Inference, "inference",
     {"kubeconfig_path": KC}),
    ("gateway", mcp_gateway.build_gateway_plan, "gateway", Gateway, "gateway",
     {"kubeconfig_path": KC, "upstream_url": "http://vllm.inference.svc:8000", "api_key_secret": "s",
      "org_cp_internal_url": "http://organization-control-plane.control-plane.svc:8000"}),
    ("policy", mcp_policy.build_policy_plan, "policy", Policy, "policy",
     {"kubeconfig_path": KC, "cache_url": "redis://v.platform.svc:6379/0", "allow_no_backbone": True}),
    ("ingress_gateway", mcp_ingress_gateway.build_ingress_gateway_plan, "gateway", IngressGateway, "ingress-gateway",
     {"kubeconfig_path": KC, "address_pool": ["192.168.1.240-192.168.1.250"]}),
    ("valkey", mcp_valkey.build_valkey_plan, "valkey", Cache, "valkey",
     {"kubeconfig_path": KC, "name": "v"}),
    ("cnpg", mcp_cnpg.build_cnpg_plan, "cnpg", Database, "cnpg",
     {"kubeconfig_path": KC, "name": "db", "database": {"name": "d", "owner": "o", "password": "p"}}),
    ("observability", mcp_observability.build_observability_plan, "observability", Observability, "observability",
     {"kubeconfig_path": KC, "storage_class": "longhorn"}),
    ("tokenizer", mcp_tokenizer.build_tokenizer_plan, "tokenizer", Tokenizer, "tokenizer",
     {"kubeconfig_path": KC}),
    ("controlplane", mcp_controlplane.build_controlplane_plan, "control_plane", ControlPlane, "controlplane",
     {"kubeconfig_path": KC, "type": "admin", "existing_secret": "s"}),
    ("portal", mcp_portal.build_portal_plan, "portal", Portal, "portal",
     {"kubeconfig_path": KC, "type": "admin", "api_upstream": "http://admin-control-plane.platform.svc:8000"}),
    ("enricher", mcp_enricher.build_enricher_plan, "enricher", Enricher, "enricher",
     {"kubeconfig_path": KC, "event_backbone_url": "nats://nats.platform.svc:4222"}),
    ("billing", mcp_billing.build_billing_plan, "billing", Billing, "billing",
     {"kubeconfig_path": KC, "existing_secret": "billing-secrets", "event_backbone_url": "nats://nats.platform.svc:4222"}),
    ("route", mcp_route.build_route_plan, "route", Route, "route",
     {"kubeconfig_path": KC, "name": "gw-route", "namespace": "gateway",
       "service": "gateway-model-gateway", "port": 8080}),
    ("accelerator", mcp_accelerator.build_accelerator_plan, "accelerator", Accelerator, "accelerator",
     {"kubeconfig_path": KC}),
    ("queue", mcp_queue.build_queue_plan, "queue", Queue, "queue",
     {"kubeconfig_path": KC, "storage_class": "longhorn"}),
]

# Tools that legitimately take one extra argument beyond their spec.
# Both are real SDK signatures, not hand-listed spec fields:
# StorageBackend.create(storage, nodes=...) needs them, and
# InferenceBackend.create(inference, nodes=...) uses them to check
# prerequisites / pick a CPU dtype.
EXTRA_ARGS = {"storage": {"nodes"}, "inference": {"nodes"}}

IDS = [c[0] for c in CAPABILITIES]


def _skill_text(skill_dir: str) -> str:
    return (SKILLS_DIR / skill_dir / "SKILL.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("cap", CAPABILITIES, ids=IDS)
def test_tool_takes_the_real_spec_type_not_hand_listed_fields(cap):
    """Each tool takes the real SDK spec as ONE argument, so every
    current and future field (and default) is automatically part of the
    schema the model sees, with nothing to edit here when the SDK adds
    one. If this fails, someone went back to re-listing fields
    individually, reintroducing exactly the drift this design removes
    structurally."""
    name, func, argname, spec, _skill, _minimal = cap
    # get_type_hints(), not inspect.signature() -- these modules use
    # `from __future__ import annotations`, so a raw signature's
    # .annotation is just the unevaluated string.
    hints = typing.get_type_hints(func)
    assert set(hints) == {argname, "return"} | EXTRA_ARGS.get(name, set())
    assert hints[argname] is spec


@pytest.mark.parametrize("cap", CAPABILITIES, ids=IDS)
def test_every_spec_field_is_documented_in_skill_md(cap):
    """SKILL.md's prose is hand-maintained even though the tool's schema
    is auto-derived -- this is what keeps the prose from drifting off the
    real field set. If this fails, the SDK gained (or renamed) a field:
    document it in SKILL.md, don't delete the assertion."""
    _name, _func, _argname, spec, skill, _minimal = cap
    text = _skill_text(skill)
    undocumented = [f for f in spec.model_fields if f not in text]
    assert not undocumented, f"{spec.__name__} fields missing from {skill}/SKILL.md: {undocumented}"


@pytest.mark.parametrize("cap", CAPABILITIES, ids=IDS)
def test_every_per_type_options_class_is_named_in_skill_md(cap):
    """A capability with more than one `type` has a different options
    class per type, and they differ in more than their name (Portal's
    two carry different container images). The model can only get that
    right if SKILL.md actually names them."""
    _name, _func, _argname, spec, skill, _minimal = cap
    text = _skill_text(skill)
    missing = [
        cls.__name__
        for cls in getattr(spec, "OPTIONS_FOR_TYPE", {}).values()
        if cls.__name__ not in text
    ]
    assert not missing, f"options classes missing from {skill}/SKILL.md: {missing}"


@pytest.mark.parametrize("cap", CAPABILITIES, ids=IDS)
def test_tool_is_registered_for_the_orchestrator(cap):
    """graph.py picks up whatever is in ALL_TOOLS -- a tool module that
    exists but was never added there is invisible to the model."""
    _name, func, _argname, _spec, _skill, _minimal = cap
    assert func.__name__ in {t.name for t in ALL_TOOLS}


def _secret_fields(spec) -> list[str]:
    return [k for k, f in spec.model_fields.items() if "SecretStr" in str(f.annotation)]


@pytest.mark.parametrize("cap", CAPABILITIES, ids=IDS)
def test_secret_fields_render_their_real_value(cap):
    """Every SecretStr field the SDK has -- including ones added after this
    was written -- must reach the script as its real value. A masked one
    renders as SecretStr('**********'), which is valid Python and passes
    every other check here, and the service then authenticates with
    asterisks. That happened twice: cache_auth_url, then
    admin_cp_service_api_key."""
    name, func, argname, spec, _skill, minimal = cap
    secrets = {k: f"real-{k}" for k in _secret_fields(spec)}
    kwargs = {argname: {**minimal, **secrets}}
    if name == "storage":
        kwargs["nodes"] = NODES
    result = func(**kwargs)
    assert result["valid"] is True, result
    assert "**********" not in result["script"]
    for value in secrets.values():
        assert repr(value) in result["script"]


def test_full_stack_renders_secret_fields_unmasked():
    policy = {
        "kubeconfig_path": KC, "cache_url": "redis://v.platform.svc:6379/0", "allow_no_backbone": True,
        **{k: f"real-{k}" for k in _secret_fields(Policy)},
    }
    database = {"kubeconfig_path": KC, "name": "db",
                "database": {"name": "d", "owner": "o", "password": "real-db-password"}}
    result = mcp_full_stack.build_full_stack_plan(
        cluster={"name": "c", "nodes": NODES}, policy=policy, cnpg=database,
        valkey={"kubeconfig_path": KC, "name": "v"},
        controlplane={"kubeconfig_path": KC, "type": "organization", "existing_secret": "s"},
        gateway={"kubeconfig_path": KC, "upstream_url": "http://vllm.inference.svc:8000",
                 "api_key_secret": "s", "org_cp_internal_url": "wired"})
    assert result["valid"] is True, result
    script = result["script"]
    assert "**********" not in script
    assert "'real-db-password'" in script
    for k in _secret_fields(Policy):
        assert repr(f"real-{k}") in script


@pytest.mark.parametrize("cap", CAPABILITIES, ids=IDS)
def test_generated_script_only_names_things_the_sdk_really_has(cap):
    """The rendered script is TEXT -- an SDK rename can't break it until
    someone actually runs it. So render a real one, execute only its
    import lines, and check every class it constructs and every backend
    method it calls still exists.

    This is the check that would catch `DatabaseBackend.create_cluster` or
    `StorageBackend.install_prerequisites` being renamed, neither of
    which any other test here would notice."""
    name, func, argname, _spec, _skill, minimal = cap
    kwargs = {argname: minimal}
    if name == "storage":
        kwargs["nodes"] = NODES
    result = func(**kwargs)
    assert result["valid"] is True, result
    script = result["script"]

    tree = ast.parse(script)
    ns: dict = {}
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            # Executes only the import lines -- an unresolvable name or a
            # class the SDK no longer exports raises right here.
            exec(compile(ast.Module(body=[node], type_ignores=[]), "<imports>", "exec"), ns)

    # var -> the class it was constructed from, e.g. backend = DatabaseBackend()
    built: dict[str, type] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            callee = node.value.func
            if isinstance(callee, ast.Name) and callee.id in ns:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        built[target.id] = ns[callee.id]

    checked = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            base = node.func.value
            if isinstance(base, ast.Name) and base.id in built:
                owner = built[base.id]
                method = node.func.attr
                assert hasattr(owner, method), (
                    f"{name}: generated script calls {owner.__name__}.{method}(), "
                    "which no longer exists on the real SDK class"
                )
                checked += 1
    assert checked, f"{name}: no backend calls found to verify in the generated script"


def test_full_stack_composes_every_capability_that_installs_into_a_cluster():
    """build_full_stack_plan is the one tool allowed to know about more
    than one capability. Every spec with a standalone tool should also be
    composable there, or a user who asks for a whole platform silently
    gets a smaller one than they asked for."""
    hints = typing.get_type_hints(mcp_full_stack.build_full_stack_plan)
    composed = set()
    for key, hint in hints.items():
        if key == "return":
            continue
        # Optional[X] -> X; the cluster arg is bare RKE2Cluster.
        args = typing.get_args(hint)
        composed.add(args[0] if args else hint)

    for _name, _func, _argname, spec, _skill, _minimal in CAPABILITIES:
        assert spec in composed, f"{spec.__name__} has a standalone tool but isn't composable in build_full_stack_plan"


def _declared_types(text: str) -> dict:
    """{class name: {field: declaration}} from SKILL.md's class blocks, so a
    field is checked against the class it is actually listed under."""
    blocks, current = {}, None
    for line in text.splitlines():
        header = re.match(r"class (\w+)\(", line)
        if header:
            current = blocks.setdefault(header.group(1), {})
            continue
        field = re.match(r"    (\w+)\s*:\s*(.+)$", line)
        if current is not None and field:
            current[field.group(1)] = field.group(2)
        elif line.startswith("```"):
            current = None
    return blocks


@pytest.mark.parametrize("cap", CAPABILITIES, ids=IDS)
def test_skill_md_never_shows_a_defaulted_field_as_required(cap):
    """The enricher's skill showed `max_deliver: int` with no default, and
    the model asked users for ports, retry counts and timeouts that all
    had defaults. A field the SDK defaults must show `= <default>`."""
    _name, _func, _argname, spec, skill, _minimal = cap
    blocks = _declared_types(_skill_text(skill))
    models = [spec, *getattr(spec, "OPTIONS_FOR_TYPE", {}).values()]
    wrong = []
    for model in dict.fromkeys(models):
        declared = blocks.get(model.__name__, {})
        for field, info in model.model_fields.items():
            shown = declared.get(field)
            if shown is not None and not info.is_required() and "=" not in shown.split("#")[0]:
                wrong.append(f"{model.__name__}.{field} (defaults to {info.default!r})")
    assert not wrong, f"{skill}/SKILL.md shows defaulted fields as required: {wrong}"
