"""Tests for the whole-stack MCP tool (agentic/backend/src/mcp_tools/full_stack.py).
Calls the tool function directly as plain Python -- no MCP transport
involved."""

from unittest.mock import patch

import pytest
from pydantic import ValidationError

from mcp_tools.full_stack import build_full_stack_plan
from multistack.backends.minio_client import MinIOBackend
from multistack.backends.rke2_client import RKE2Backend
from multistack.database import DatabaseBackend
from multistack.cache import CacheBackend
from multistack.controlplane.registry import ControlPlaneBackend
from multistack.enricher.registry import EnricherBackend
from multistack.gateway.registry import GatewayBackend
from multistack.inference.registry import InferenceBackend
from multistack.ingress_gateway.registry import IngressGatewayBackend
from multistack.observability.registry import ObservabilityBackend
from multistack.policy.registry import PolicyBackend
from multistack.portal.registry import PortalBackend
from multistack.storage.registry import StorageBackend
from multistack.tokenizer.registry import TokenizerBackend

BASE_CLUSTER = {"name": "ai-cluster", "nodes": [{"address": "10.0.0.11", "role": "server"}], "cni": "cilium"}
STORAGE_ARGS = {"type": "longhorn", "kubeconfig_path": "placeholder", "replica_count": 1}
MINIO_ARGS = {
    "kubeconfig_path": "placeholder",
    "name": "minio-test",
    "storage_class": "placeholder",
    "servers": 2,
    "volumes_per_server": 2,
}
GATEWAY_ARGS = {
    "kubeconfig_path": "placeholder",
    "upstream_url": "http://vllm-service.inference.svc:8000",
    "api_key_secret": "gateway-keys",
    "org_cp_internal_url": "http://organization-control-plane.control-plane.svc:8000",
}
POLICY_ARGS = {
    "kubeconfig_path": "placeholder",
    "cache_url": "redis://valkey.platform.svc:6379/0",
    "allow_no_backbone": True,
    "limits": {"user_default": 60},
}
INGRESS_ARGS = {
    "kubeconfig_path": "placeholder",
    "address_pool": ["192.168.1.240-192.168.1.250"],
}
INFERENCE_ARGS = {
    "kubeconfig_path": "placeholder",
    "model": "Qwen/Qwen2.5-0.5B-Instruct",
}
VALKEY_ARGS = {
    "kubeconfig_path": "placeholder",
    "name": "platform-cache",
}
CNPG_ARGS = {
    "kubeconfig_path": "placeholder",
}
CNPG_WITH_DB_ARGS = {
    "kubeconfig_path": "placeholder",
    "name": "app-db",
    "database": {"name": "appdb", "owner": "appuser", "password": "hunter2"},
}
OBSERVABILITY_ARGS = {
    "kubeconfig_path": "placeholder",
    "storage_class": "placeholder",
}
TOKENIZER_ARGS = {
    "kubeconfig_path": "placeholder",
}
TPM_POLICY_ARGS = {
    "kubeconfig_path": "placeholder",
    "cache_url": "redis://valkey.platform.svc:6379/0",
    "allow_no_backbone": True,
    "type": "tpm",
    "limits": {"user_default": 60},
}
CONTROLPLANE_ADMIN_ARGS = {
    "kubeconfig_path": "placeholder",
    "type": "admin",
    "existing_secret": "admin-cp-secrets",
}
CONTROLPLANE_ORG_ARGS = {
    "kubeconfig_path": "placeholder",
    "type": "organization",
    "existing_secret": "org-cp-secrets",
}
# Gateway's own baseline (see build_full_stack_plan's dependency check):
# every request is verified synchronously against the organization
# control plane, which itself requires a real database and cache. Splat
# this into any test that composes `gateway` and isn't specifically
# testing the rejection when it's missing.
GATEWAY_BASELINE_KWARGS = {
    "cnpg": CNPG_WITH_DB_ARGS,
    "valkey": VALKEY_ARGS,
    "controlplane": CONTROLPLANE_ORG_ARGS,
}
PORTAL_ADMIN_ARGS = {
    "kubeconfig_path": "placeholder",
    "type": "admin",
    "api_upstream": "http://admin-control-plane.platform.svc:8000",
}

QUEUE_ARGS = {"kubeconfig_path": "placeholder"}

# The architecture's dependency chains (see mcp_tools/architecture.py),
# splatted into any test that composes a component needing them and isn't
# testing the rejection when they're missing.
# The request path: Postgres + Redis + organization control plane + gateway.
REQUEST_PATH_KWARGS = {**GATEWAY_BASELINE_KWARGS, "gateway": GATEWAY_ARGS}
# A rate limiter's own needs. POLICY_ARGS opts out of the backbone, so no queue.
POLICY_DEPS = REQUEST_PATH_KWARGS
# The enricher: the request path, the event backbone and the storage it
# persists to, and the tokenizer as the enricher's fallback.
ENRICHER_DEPS = {
    **REQUEST_PATH_KWARGS,
    "storage": STORAGE_ARGS,
    "queue": QUEUE_ARGS,
    "tokenizer": {"kubeconfig_path": "placeholder"},
}


def run_script(script: str, **returns) -> dict:
    """Executes a generated script with every backend faked, returning its
    namespace. `returns` overrides what a backend's create() returns, by
    class name."""
    from contextlib import ExitStack

    from multistack import BillingBackend, QueueBackend

    backends = [
        RKE2Backend, StorageBackend, MinIOBackend, InferenceBackend, CacheBackend,
        DatabaseBackend, ObservabilityBackend, QueueBackend, TokenizerBackend,
        EnricherBackend, PolicyBackend, GatewayBackend, IngressGatewayBackend,
        ControlPlaneBackend, PortalBackend, BillingBackend,
    ]
    with ExitStack() as fakes:
        for cls in backends:
            default = "/tmp/fake-kubeconfig.yaml" if cls is RKE2Backend else None
            fakes.enter_context(patch.object(cls, "create", return_value=returns.get(cls.__name__, default)))
        fakes.enter_context(patch.object(DatabaseBackend, "create_cluster", return_value=None))
        fakes.enter_context(patch.object(StorageBackend, "install_prerequisites", return_value=None))
        namespace: dict = {}
        exec(compile(script, "<generated>", "exec"), namespace)
    return namespace


def test_cluster_only_needs_no_other_layer():
    result = build_full_stack_plan(cluster=BASE_CLUSTER)
    assert result["valid"] is True
    assert result["layers"] == ["rke2"]
    assert "Stack(" in result["script"]
    assert "Storage" not in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_cluster_and_storage():
    result = build_full_stack_plan(cluster=BASE_CLUSTER, storage=STORAGE_ARGS)
    assert result["valid"] is True
    assert result["layers"] == ["rke2", "storage"]
    assert "stack.build(\n    Storage," in result["script"]
    assert "MinIOTenant" not in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_minio_without_storage_is_rejected():
    result = build_full_stack_plan(cluster=BASE_CLUSTER, minio=MINIO_ARGS)
    assert result["valid"] is False
    assert "storage" in result["error"]


def test_cluster_only_has_no_gateway_or_policy_endpoint():
    result = build_full_stack_plan(cluster=BASE_CLUSTER)
    assert "gateway_endpoint" not in result
    assert "policy_endpoint" not in result


def test_gateway_and_policy_endpoints_are_real_computed_values():
    # Regression test: the model was observed inventing a wrong endpoint
    # (using the cluster name instead of the real release-name-based
    # service name) when this tool gave it nothing real to quote. These
    # are deterministic properties -- no real backend call needed.
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, gateway=GATEWAY_ARGS, policy=POLICY_ARGS, **GATEWAY_BASELINE_KWARGS,
    )
    assert result["gateway_endpoint"] == "http://gateway-model-gateway.gateway.svc:8080"
    assert result["policy_endpoint"] == "http://rpm-rate-limiter-rpm.policy.svc:8000/check"


def test_gateway_requires_an_organization_control_plane():
    """Every request is verified synchronously against it
    (org_cp_internal_url); without one, gateway is deployed, healthy,
    and rejects every real request. Unlike minio's storage requirement,
    this cascades: composing an organization controlplane already
    requires a real database and cache (see that check), which is the
    platform's own baseline satisfied by this one rule."""
    result = build_full_stack_plan(cluster=BASE_CLUSTER, gateway=GATEWAY_ARGS)
    assert result["valid"] is False
    assert "organization control plane" in result["error"]


def test_gateway_with_an_admin_only_control_plane_is_still_rejected():
    result = build_full_stack_plan(cluster=BASE_CLUSTER, gateway=GATEWAY_ARGS, controlplane=CONTROLPLANE_ADMIN_ARGS)
    assert result["valid"] is False
    assert "organization control plane" in result["error"]


def test_gateway_with_its_baseline_composes():
    result = build_full_stack_plan(cluster=BASE_CLUSTER, gateway=GATEWAY_ARGS, **GATEWAY_BASELINE_KWARGS)
    assert result["valid"] is True
    assert "stack.build(\n    Gateway," in result["script"]
    assert "gateway-keys" in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_gateway_with_both_control_planes_composed_is_not_rejected():
    # Admin AND organization together still satisfy the requirement --
    # it's specifically "at least one organization type", not "exactly
    # the organization type alone".
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, gateway=GATEWAY_ARGS, cnpg=CNPG_WITH_DB_ARGS, valkey=VALKEY_ARGS,
        controlplane=[CONTROLPLANE_ADMIN_ARGS, CONTROLPLANE_ORG_ARGS],
    )
    assert result["valid"] is True


def test_gateway_placeholder_kubeconfig_never_leaks_but_api_key_secret_is_real():
    # kubeconfig_path is a genuine placeholder (Stack fills it for real);
    # api_key_secret is NOT a placeholder even though it's also in
    # Gateway.FROM_STACK, since this tool builds no Secret -- the real
    # value the caller passed must survive into the script untouched.
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER,
        gateway={**GATEWAY_ARGS, "kubeconfig_path": "SENTINEL-GATEWAY-KC"},
        **GATEWAY_BASELINE_KWARGS,
    )
    assert result["valid"] is True
    assert "SENTINEL-GATEWAY-KC" not in result["script"]
    assert "gateway-keys" in result["script"]


def test_policy_alone_is_rejected_naming_the_gateway():
    """A rate limiter is called by the gateway -- on its own it limits
    nothing, so the plan must carry the request path too."""
    result = build_full_stack_plan(cluster=BASE_CLUSTER, policy=POLICY_ARGS)
    assert result["valid"] is False
    assert "policy needs gateway" in result["error"]


def test_policy_composes_with_its_request_path():
    result = build_full_stack_plan(cluster=BASE_CLUSTER, policy=POLICY_ARGS, **POLICY_DEPS)
    assert result["valid"] is True
    assert result["layers"] == ["rke2", "valkey", "cnpg", "controlplane", "policy", "gateway"]
    assert "stack.build(\n    Policy," in result["script"]
    assert "RateLimits(user_default=60" in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_policy_placeholder_kubeconfig_never_leaks():
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER,
        policy={**POLICY_ARGS, "kubeconfig_path": "SENTINEL-POLICY-KC"},
        **POLICY_DEPS,
    )
    assert result["valid"] is True
    assert "SENTINEL-POLICY-KC" not in result["script"]


def test_policy_tpm_imports_tpmoptions_not_rpmoptions():
    # Same regression as mcp_tools/policy.py's own test -- full_stack.py
    # had its own hardcoded "from multistack.policy import RPMOptions".
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER,
        enricher={"kubeconfig_path": "placeholder", "event_backbone_url": "nats://nats.platform.svc:4222"},
        policy={
            **POLICY_ARGS,
            "type": "tpm",
            "options": {"durable_name": "tpm-counter-enriched"},
        },
        **ENRICHER_DEPS,
    )
    assert result["valid"] is True
    assert "from multistack.policy import TPMOptions" in result["script"]
    assert "RPMOptions" not in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_policy_rejects_no_backbone_without_explicit_opt_out():
    # Same as the standalone build_policy_plan: Policy's own
    # model_validator runs during argument construction (via
    # @validate_call), before build_full_stack_plan's body ever
    # executes -- so this is a raised ValidationError, not a
    # {"valid": False} return.
    with pytest.raises(ValidationError, match="allow_no_backbone"):
        build_full_stack_plan(
            cluster=BASE_CLUSTER,
            policy={
                "kubeconfig_path": "placeholder",
                "cache_url": "redis://valkey.platform.svc:6379/0",
                "limits": {"user_default": 60},
            },
        )


def test_valkey_needs_no_other_layer():
    # Same as gateway/policy/ingress -- valkey only REQUIRES the cluster.
    result = build_full_stack_plan(cluster=BASE_CLUSTER, valkey=VALKEY_ARGS)
    assert result["valid"] is True
    assert result["layers"] == ["rke2", "valkey"]
    assert "stack.build(\n    Cache," in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_valkey_placeholder_kubeconfig_never_leaks_but_name_is_real():
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER,
        valkey={**VALKEY_ARGS, "kubeconfig_path": "SENTINEL-VALKEY-KC"},
    )
    assert result["valid"] is True
    assert "SENTINEL-VALKEY-KC" not in result["script"]
    assert "platform-cache" in result["script"]


def test_valkey_and_policy_together_auto_wire_cache_url():
    # Cache.PROVIDES a real cache_url -- composing it with policy excludes
    # cache_url from Policy's explicit render (same placeholder treatment
    # as kubeconfig_path) so Stack fills in the real cache endpoint.
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER,
        policy={**POLICY_ARGS, "cache_url": "placeholder"},
        **POLICY_DEPS,
    )
    assert result["valid"] is True
    policy_call = result["script"].split("stack.build(\n    Policy,")[1].split(")")[0]
    assert "cache_url" not in policy_call
    compile(result["script"], "<rendered>", "exec")


def test_policy_placeholder_cache_url_never_reaches_the_script():
    """With valkey composed -- which a policy now always has, through its
    request path -- the passed cache_url is a placeholder and is dropped."""
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, policy={**POLICY_ARGS, "cache_url": "SENTINEL-CACHE"}, **POLICY_DEPS,
    )
    assert result["valid"] is True
    assert "SENTINEL-CACHE" not in result["script"]


def test_cnpg_operator_only_needs_no_other_layer():
    # Same as gateway/policy/ingress/valkey -- cnpg only REQUIRES the cluster.
    result = build_full_stack_plan(cluster=BASE_CLUSTER, cnpg=CNPG_ARGS)
    assert result["valid"] is True
    assert result["layers"] == ["rke2", "cnpg"]
    assert "stack.build(\n    Database," in result["script"]
    assert "database_url" not in result
    assert "create_cluster" not in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_cnpg_placeholder_kubeconfig_never_leaks():
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER,
        cnpg={**CNPG_ARGS, "kubeconfig_path": "SENTINEL-CNPG-KC"},
    )
    assert result["valid"] is True
    assert "SENTINEL-CNPG-KC" not in result["script"]


def test_cnpg_with_database_renders_the_real_password_and_computes_url():
    # database.password is NEVER a placeholder -- this tool builds no
    # Secret, so the real value the caller gave must survive untouched.
    result = build_full_stack_plan(cluster=BASE_CLUSTER, cnpg=CNPG_WITH_DB_ARGS)
    assert result["valid"] is True
    assert result["database_url"] == "postgresql://appuser@app-db-rw.postgres.svc.cluster.local:5432/appdb"
    assert "DatabaseConfig(name='appdb', owner='appuser', password='hunter2')" in result["script"]
    assert "database_backend.create_cluster(database)" in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_cnpg_database_without_name_is_rejected():
    # Constructing a Database checks the operator half only -- the
    # cluster half is optional -- so build_full_stack_plan must call
    # validate_cluster() explicitly, same as the standalone
    # build_cnpg_plan does.
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER,
        cnpg={
            "kubeconfig_path": "placeholder",
            "database": {"name": "appdb", "owner": "appuser", "password": "hunter2"},
        },
    )
    assert result["valid"] is False
    assert "name is required" in result["error"]


def test_cnpg_never_auto_wires_to_anything():
    # cnpg has no consumer among the other composable layers -- confirm
    # combining it with gateway/policy doesn't change either of them.
    # gateway now needs its own baseline (an organization controlplane,
    # which itself needs a cache -- cnpg is already given here, standing
    # in for the database half of that baseline).
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, cnpg=CNPG_WITH_DB_ARGS, valkey=VALKEY_ARGS,
        controlplane=CONTROLPLANE_ORG_ARGS, gateway=GATEWAY_ARGS, policy=POLICY_ARGS,
    )
    assert result["valid"] is True
    assert result["layers"] == ["rke2", "valkey", "cnpg", "controlplane", "policy", "gateway"]
    # Neither gateway's nor policy's own constructor call names cnpg or
    # a database value anywhere -- confirms cnpg has no consumer here.
    gateway_call = result["script"].split("stack.build(\n    Gateway,")[1].split(")\n")[0]
    policy_call = result["script"].split("stack.build(\n    Policy,")[1].split(")\n")[0]
    assert "database" not in gateway_call and "database" not in policy_call
    compile(result["script"], "<rendered>", "exec")


def test_gateway_policy_endpoints_wire_from_a_composed_policy():
    """Gateway.policy_endpoints has no FROM_STACK entry (Stack.build()
    can't fill it), so this tool wires it explicitly by referencing the
    already-built policy variable -- which is why policy now renders
    BEFORE gateway in the script, not the other way around."""
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, gateway=GATEWAY_ARGS, policy=POLICY_ARGS, **GATEWAY_BASELINE_KWARGS,
    )
    assert result["valid"] is True
    assert result["script"].index("stack.build(\n    Policy,") < result["script"].index("stack.build(\n    Gateway,")
    assert "policy_endpoints=[policy.endpoint]" in result["script"]
    compile(result["script"], "<rendered>", "exec")

    with patch.object(RKE2Backend, "create", return_value="/tmp/fake-kubeconfig.yaml"), \
         patch.object(CacheBackend, "create", return_value=None), \
         patch.object(DatabaseBackend, "create", return_value=None), \
         patch.object(DatabaseBackend, "create_cluster", return_value=None), \
         patch.object(PolicyBackend, "create", return_value=None), \
         patch.object(GatewayBackend, "create", return_value=None), \
         patch.object(ControlPlaneBackend, "create", return_value=None):
        namespace = {}
        exec(compile(result["script"], "<generated>", "exec"), namespace)

    assert namespace["gateway"].policy_endpoints == [namespace["policy"].endpoint]


def test_gateway_policy_endpoints_not_overridden_when_given_explicitly():
    """'Explicit beats wired': a caller who already named a real endpoint
    (attaching to a policy running outside this call) is not missing
    anything, and this tool must not override it."""
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER,
        gateway={**GATEWAY_ARGS, "policy_endpoints": ["http://external-limiter.example.com/check"]},
        policy=POLICY_ARGS,
        **GATEWAY_BASELINE_KWARGS,
    )
    assert result["valid"] is True
    assert "policy_endpoints=['http://external-limiter.example.com/check']" in result["script"]
    assert "policy.endpoint" not in result["script"].split("Gateway,")[1].split(")")[0]
    compile(result["script"], "<rendered>", "exec")


def test_ingress_needs_no_other_layer():
    # Same as gateway/policy -- ingress only REQUIRES the cluster.
    result = build_full_stack_plan(cluster=BASE_CLUSTER, ingress=INGRESS_ARGS)
    assert result["valid"] is True
    assert result["layers"] == ["rke2", "ingress"]
    assert "stack.build(\n    IngressGateway," in result["script"]
    assert "from multistack.ingress_gateway import IngressGateway, IngressGatewayBackend" in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_ingress_placeholder_kubeconfig_never_leaks_but_address_pool_is_real():
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER,
        ingress={**INGRESS_ARGS, "kubeconfig_path": "SENTINEL-INGRESS-KC"},
    )
    assert result["valid"] is True
    assert "SENTINEL-INGRESS-KC" not in result["script"]
    assert "192.168.1.240-192.168.1.250" in result["script"]


def test_ingress_has_no_endpoint_in_the_return_value():
    # Unlike gateway/policy, ingress's external address isn't a
    # deterministic property -- MetalLB only assigns one at real deploy
    # time, so there's nothing correct to return here.
    result = build_full_stack_plan(cluster=BASE_CLUSTER, ingress=INGRESS_ARGS)
    assert "ingress_endpoint" not in result
    assert "external_endpoint" not in result


def test_gateway_policy_and_ingress_together():
    # gateway<-policy DOES wire (see the dedicated tests above); ingress
    # is the genuinely independent one here -- it provisions the front
    # door itself and doesn't route gateway's traffic through it.
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, gateway=GATEWAY_ARGS, policy=POLICY_ARGS, ingress=INGRESS_ARGS,
        **GATEWAY_BASELINE_KWARGS,
    )
    assert result["valid"] is True
    assert result["script"].index("stack.build(\n    Policy,") < result["script"].index("stack.build(\n    Gateway,")
    assert "ingress" in result["layers"]
    compile(result["script"], "<rendered>", "exec")


def test_inference_needs_no_other_layer():
    # Same as gateway/policy/ingress -- inference only REQUIRES the cluster.
    result = build_full_stack_plan(cluster=BASE_CLUSTER, inference=INFERENCE_ARGS)
    assert result["valid"] is True
    assert result["layers"] == ["rke2", "inference"]
    assert "stack.build(\n    Inference," in result["script"]
    assert result["inference_endpoint"] == "http://vllm.inference.svc.cluster.local:8000"
    compile(result["script"], "<rendered>", "exec")


def test_inference_placeholder_kubeconfig_never_leaks_but_model_is_real():
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER,
        inference={**INFERENCE_ARGS, "kubeconfig_path": "SENTINEL-INFERENCE-KC"},
    )
    assert result["valid"] is True
    assert "SENTINEL-INFERENCE-KC" not in result["script"]
    assert "Qwen/Qwen2.5-0.5B-Instruct" in result["script"]


def test_inference_without_minio_renders_s3_fields_as_literal_none():
    # No minio composed and no s3 model -- s3_endpoint_url/s3_secret_name
    # have no placeholder role to play here, so they render as their
    # real (unset) value, same as any other field would.
    result = build_full_stack_plan(cluster=BASE_CLUSTER, inference=INFERENCE_ARGS)
    assert result["valid"] is True
    assert "s3_endpoint_url=None" in result["script"]
    assert "s3_secret_name=None" in result["script"]


def test_inference_with_minio_and_s3_model_treats_s3_endpoint_url_as_a_placeholder():
    # The one case s3_endpoint_url joins kubeconfig_path as a REQUIRED
    # placeholder: minio also composed, model is an s3:// path. The SDK
    # requires a real-looking value at construction, so the caller still
    # passes one (here, the same "placeholder" sentinel used for
    # kubeconfig_path elsewhere) -- but it must never survive into the
    # rendered script, since Stack fills the real one from the recorded
    # MinIO tenant instead. s3_secret_name is NEVER a placeholder, so
    # the real value given for it must survive.
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, storage=STORAGE_ARGS, minio=MINIO_ARGS,
        inference={
            **INFERENCE_ARGS,
            "model": "s3://models/Qwen2.5-0.5B",
            "s3_endpoint_url": "placeholder",
            "s3_secret_name": "minio-creds",
        },
    )
    assert result["valid"] is True
    assert "placeholder" not in result["script"]
    assert "s3_secret_name='minio-creds'" in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_inference_with_minio_but_non_s3_model_never_treats_s3_endpoint_url_as_a_placeholder():
    # minio being present is NOT enough on its own -- a non-s3 model
    # must never get an auto-wired s3_endpoint_url, since the real SDK
    # rejects s3_endpoint_url being set on a non-s3 model outright.
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, storage=STORAGE_ARGS, minio=MINIO_ARGS, inference=INFERENCE_ARGS,
    )
    assert result["valid"] is True
    assert "s3_endpoint_url=None" in result["script"]
    assert "s3_secret_name=None" in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_inference_with_s3_model_but_no_minio_renders_s3_endpoint_url_literally():
    # Without minio composed, there's nothing to auto-wire s3_endpoint_url
    # from at all -- even for an s3:// model, it's a real field like any
    # other and the caller's real value must survive as-is.
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER,
        inference={
            **INFERENCE_ARGS,
            "model": "s3://models/Qwen2.5-0.5B",
            "s3_endpoint_url": "https://external-minio.example.com",
            "s3_secret_name": "external-creds",
        },
    )
    assert result["valid"] is True
    assert "s3_endpoint_url='https://external-minio.example.com'" in result["script"]
    assert "s3_secret_name='external-creds'" in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_placeholder_kubeconfig_and_storage_class_never_appear_in_the_script():
    # The whole point of the FROM_STACK-based exclusion (see
    # full_stack.py's module docstring): whatever placeholder the caller
    # passes for storage.kubeconfig_path / minio.kubeconfig_path /
    # minio.storage_class must never leak into the generated script --
    # Stack supplies the real values at run time instead.
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER,
        storage={**STORAGE_ARGS, "kubeconfig_path": "SENTINEL-STORAGE-KC"},
        minio={**MINIO_ARGS, "kubeconfig_path": "SENTINEL-MINIO-KC", "storage_class": "SENTINEL-STORAGE-CLASS"},
    )
    assert result["valid"] is True
    assert "SENTINEL" not in result["script"]


def test_full_stack_script_actually_executes_with_correct_wiring():
    # Stronger than a syntax check: actually run the generated script
    # (with only the real infra calls stubbed) and confirm Stack really
    # threads the cluster's kubeconfig and storage's StorageClass through
    # to the next layer, not the placeholders that were passed in.
    result = build_full_stack_plan(cluster=BASE_CLUSTER, storage=STORAGE_ARGS, minio=MINIO_ARGS)
    assert result["valid"] is True

    with patch.object(RKE2Backend, "create", return_value="/tmp/fake-kubeconfig.yaml"), \
         patch.object(StorageBackend, "install_prerequisites", return_value=None), \
         patch.object(StorageBackend, "create", return_value="longhorn"), \
         patch.object(MinIOBackend, "create", return_value={"endpoint": "https://minio.minio.svc.cluster.local"}):
        namespace = {}
        exec(compile(result["script"], "<generated>", "exec"), namespace)

    assert namespace["storage"].kubeconfig_path == "/tmp/fake-kubeconfig.yaml"
    assert namespace["tenant"].kubeconfig_path == "/tmp/fake-kubeconfig.yaml"
    assert namespace["tenant"].storage_class == "longhorn"
    assert namespace["stack"].outputs["s3_endpoint_url"] == "https://minio.minio.svc.cluster.local"


def test_gateway_script_actually_executes_with_correct_wiring():
    result = build_full_stack_plan(cluster=BASE_CLUSTER, gateway=GATEWAY_ARGS, **GATEWAY_BASELINE_KWARGS)
    assert result["valid"] is True

    with patch.object(RKE2Backend, "create", return_value="/tmp/fake-kubeconfig.yaml"), \
         patch.object(CacheBackend, "create", return_value=None), \
         patch.object(DatabaseBackend, "create", return_value=None), \
         patch.object(DatabaseBackend, "create_cluster", return_value=None), \
         patch.object(ControlPlaneBackend, "create", return_value=None), \
         patch.object(GatewayBackend, "create", return_value="http://gateway-model-gateway.gateway.svc:8080"):
        namespace = {}
        exec(compile(result["script"], "<generated>", "exec"), namespace)

    assert namespace["gateway"].kubeconfig_path == "/tmp/fake-kubeconfig.yaml"
    assert namespace["gateway"].api_key_secret == "gateway-keys"
    assert namespace["stack"].outputs["gateway_endpoint"] == "http://gateway-model-gateway.gateway.svc:8080"


def test_gateway_upstream_url_wires_from_a_composed_inference():
    """Gateway.upstream_url has no FROM_STACK entry either -- same
    reasoning as policy_endpoints above, but this one IS a required
    field, so it follows the existing placeholder convention (like
    s3_endpoint_url/cache_url): pass any non-empty string when inference
    is also composed, and it's discarded in favor of the real one."""
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, inference=INFERENCE_ARGS,
        gateway={**GATEWAY_ARGS, "upstream_url": "SENTINEL-PLACEHOLDER"},
        **GATEWAY_BASELINE_KWARGS,
    )
    assert result["valid"] is True
    assert "SENTINEL-PLACEHOLDER" not in result["script"]
    assert "upstream_url=stack.get('inference_endpoint')" in result["script"]
    compile(result["script"], "<rendered>", "exec")

    with patch.object(RKE2Backend, "create", return_value="/tmp/fake-kubeconfig.yaml"), \
         patch.object(InferenceBackend, "create", return_value="http://vllm.inference.svc.cluster.local:8000"), \
         patch.object(CacheBackend, "create", return_value=None), \
         patch.object(DatabaseBackend, "create", return_value=None), \
         patch.object(DatabaseBackend, "create_cluster", return_value=None), \
         patch.object(ControlPlaneBackend, "create", return_value=None), \
         patch.object(GatewayBackend, "create", return_value=None):
        namespace = {}
        exec(compile(result["script"], "<generated>", "exec"), namespace)

    assert namespace["gateway"].upstream_url == namespace["inference"].endpoint


def test_gateway_upstream_url_is_real_without_a_composed_inference():
    result = build_full_stack_plan(cluster=BASE_CLUSTER, gateway=GATEWAY_ARGS, **GATEWAY_BASELINE_KWARGS)
    assert result["valid"] is True
    assert f"upstream_url={GATEWAY_ARGS['upstream_url']!r}" in result["script"]


def test_policy_script_actually_executes_with_correct_wiring():
    result = build_full_stack_plan(cluster=BASE_CLUSTER, policy=POLICY_ARGS, **POLICY_DEPS)
    assert result["valid"] is True
    namespace = run_script(result["script"])
    assert namespace["policy"].kubeconfig_path == "/tmp/fake-kubeconfig.yaml"
    assert namespace["policy"].cache_url == namespace["cache"].endpoint
    assert namespace["stack"].outputs["policy_endpoint"] == namespace["policy"].endpoint


def test_ingress_script_actually_executes_with_correct_wiring():
    result = build_full_stack_plan(cluster=BASE_CLUSTER, ingress=INGRESS_ARGS)
    assert result["valid"] is True

    # Unlike Gateway/Policy (whose .endpoint is a computed property),
    # IngressGateway.external_endpoint is a real field the driver's
    # create() sets as a side effect (see
    # multistack/ingress_gateway/drivers/metallb_istio.py) -- the mock
    # has to replicate that, or stack.record() has nothing to read.
    def fake_create(self, gateway):
        gateway.external_endpoint = "203.0.113.10"
        return "203.0.113.10"

    with patch.object(RKE2Backend, "create", return_value="/tmp/fake-kubeconfig.yaml"), \
         patch.object(IngressGatewayBackend, "create", fake_create):
        namespace = {}
        exec(compile(result["script"], "<generated>", "exec"), namespace)

    assert namespace["ingress"].kubeconfig_path == "/tmp/fake-kubeconfig.yaml"
    assert namespace["ingress"].address_pool == ["192.168.1.240-192.168.1.250"]
    assert namespace["stack"].outputs["ingress_gateway_endpoint"] == "203.0.113.10"


def test_inference_script_actually_executes_with_correct_wiring():
    result = build_full_stack_plan(cluster=BASE_CLUSTER, inference=INFERENCE_ARGS)
    assert result["valid"] is True

    with patch.object(RKE2Backend, "create", return_value="/tmp/fake-kubeconfig.yaml"), \
         patch.object(InferenceBackend, "create", return_value="http://vllm.inference.svc.cluster.local:8000"):
        namespace = {}
        exec(compile(result["script"], "<generated>", "exec"), namespace)

    assert namespace["inference"].kubeconfig_path == "/tmp/fake-kubeconfig.yaml"
    assert namespace["inference"].model == "Qwen/Qwen2.5-0.5B-Instruct"
    assert namespace["stack"].outputs["inference_endpoint"] == "http://vllm.inference.svc.cluster.local:8000"


def test_inference_actually_receives_minios_real_s3_endpoint_at_runtime():
    # The real payoff of the placeholder treatment: run cluster+storage+
    # minio+inference together and confirm the Inference object Stack
    # actually builds has MinIO's real (mocked) endpoint, not the
    # placeholder string that was passed in and not None.
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, storage=STORAGE_ARGS, minio=MINIO_ARGS,
        inference={
            **INFERENCE_ARGS,
            "model": "s3://models/Qwen2.5-0.5B",
            "s3_endpoint_url": "placeholder",
            "s3_secret_name": "minio-creds",
        },
    )
    assert result["valid"] is True

    with patch.object(RKE2Backend, "create", return_value="/tmp/fake-kubeconfig.yaml"), \
         patch.object(StorageBackend, "install_prerequisites", return_value=None), \
         patch.object(StorageBackend, "create", return_value="longhorn"), \
         patch.object(MinIOBackend, "create", return_value={"endpoint": "https://minio.minio.svc.cluster.local"}), \
         patch.object(InferenceBackend, "create", return_value="http://vllm.inference.svc.cluster.local:8000"):
        namespace = {}
        exec(compile(result["script"], "<generated>", "exec"), namespace)

    assert namespace["inference"].s3_endpoint_url == "https://minio.minio.svc.cluster.local"


def test_valkey_script_actually_executes_with_correct_wiring():
    result = build_full_stack_plan(cluster=BASE_CLUSTER, valkey=VALKEY_ARGS)
    assert result["valid"] is True

    with patch.object(RKE2Backend, "create", return_value="/tmp/fake-kubeconfig.yaml"), \
         patch.object(CacheBackend, "create", return_value=None):
        namespace = {}
        exec(compile(result["script"], "<generated>", "exec"), namespace)

    assert namespace["cache"].kubeconfig_path == "/tmp/fake-kubeconfig.yaml"
    assert namespace["cache"].name == "platform-cache"


def test_policy_actually_receives_valkeys_real_cache_url_at_runtime():
    # Run the script and confirm the Policy object Stack actually builds
    # has the cache's real, computed endpoint -- not the placeholder.
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, policy={**POLICY_ARGS, "cache_url": "placeholder"}, **POLICY_DEPS,
    )
    assert result["valid"] is True
    namespace = run_script(result["script"])
    assert namespace["policy"].cache_url == namespace["cache"].endpoint
    assert namespace["policy"].cache_url.startswith("redis://platform-cache")
    assert namespace["stack"].outputs["cache_url"] == namespace["cache"].endpoint


def test_cnpg_script_actually_executes_with_correct_wiring():
    result = build_full_stack_plan(cluster=BASE_CLUSTER, cnpg=CNPG_WITH_DB_ARGS)
    assert result["valid"] is True

    with patch.object(RKE2Backend, "create", return_value="/tmp/fake-kubeconfig.yaml"), \
         patch.object(DatabaseBackend, "create", return_value=None), \
         patch.object(DatabaseBackend, "create_cluster", return_value=None):
        namespace = {}
        exec(compile(result["script"], "<generated>", "exec"), namespace)

    assert namespace["database"].kubeconfig_path == "/tmp/fake-kubeconfig.yaml"
    assert namespace["database"].name == "app-db"
    # SecretStr: the spec masks it, so read it back explicitly. The
    # rendered script still carries the literal -- that is what the
    # assertion above about result["script"] covers.
    assert namespace["database"].database.password.get_secret_value() == "hunter2"
    assert namespace["stack"].outputs["database_url"] == namespace["database"].endpoint


def test_observability_requires_storage():
    result = build_full_stack_plan(cluster=BASE_CLUSTER, observability=OBSERVABILITY_ARGS)
    assert result["valid"] is False
    assert "storage" in result["error"]


def test_observability_with_storage_needs_no_other_layer():
    result = build_full_stack_plan(cluster=BASE_CLUSTER, storage=STORAGE_ARGS, observability=OBSERVABILITY_ARGS)
    assert result["valid"] is True
    assert result["layers"] == ["rke2", "storage", "observability"]
    assert "stack.build(\n    Observability," in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_observability_placeholders_never_leak():
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER,
        storage=STORAGE_ARGS,
        observability={**OBSERVABILITY_ARGS, "kubeconfig_path": "SENTINEL-OBS-KC", "storage_class": "SENTINEL-OBS-SC"},
    )
    assert result["valid"] is True
    assert "SENTINEL-OBS-KC" not in result["script"]
    assert "SENTINEL-OBS-SC" not in result["script"]


def test_observability_script_actually_executes_with_correct_wiring():
    result = build_full_stack_plan(cluster=BASE_CLUSTER, storage=STORAGE_ARGS, observability=OBSERVABILITY_ARGS)
    assert result["valid"] is True

    with patch.object(RKE2Backend, "create", return_value="/tmp/fake-kubeconfig.yaml"), \
         patch.object(StorageBackend, "install_prerequisites", return_value=None), \
         patch.object(StorageBackend, "create", return_value=None), \
         patch.object(ObservabilityBackend, "create", return_value=None):
        namespace = {}
        exec(compile(result["script"], "<generated>", "exec"), namespace)

    assert namespace["observability"].kubeconfig_path == "/tmp/fake-kubeconfig.yaml"
    assert namespace["observability"].storage_class == namespace["stack"].outputs["storage_class"]
    assert namespace["stack"].outputs["observability_endpoint"] == namespace["observability"].grafana_endpoint()


def test_tokenizer_alone_is_rejected_naming_the_enricher():
    """The enricher is the tokenizer's only caller -- "build a tokenizer"
    used to return a lone tokenizer that counted nothing."""
    result = build_full_stack_plan(cluster=BASE_CLUSTER, tokenizer=TOKENIZER_ARGS)
    assert result["valid"] is False
    assert "tokenizer needs enricher" in result["error"]


def test_tokenizer_composes_with_its_whole_chain():
    result = build_full_stack_plan(cluster=BASE_CLUSTER, enricher=ENRICHER_ARGS, **ENRICHER_DEPS)
    assert result["valid"] is True
    assert {"queue", "tokenizer", "enricher", "gateway", "controlplane", "cnpg", "valkey"} <= set(result["layers"])
    assert result["tokenizer_url"] == "http://tokenizer.policy.svc.cluster.local:8000"
    compile(result["script"], "<rendered>", "exec")


def test_tokenizer_does_not_wire_into_an_rpm_policy():
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, enricher=ENRICHER_ARGS, policy=POLICY_ARGS, **ENRICHER_DEPS,
    )
    assert result["valid"] is True
    policy_call = result["script"].split("stack.build(\n    Policy,")[1].split("policy_backend")[0]
    assert "tokenizer_url" not in policy_call


def test_tokenizer_feeds_the_enricher_never_the_tpm_policy_directly():
    """ADR-030 moved token counting into the Enricher: it guarantees a
    count and republishes to gateway.events.enriched, and the tpm limiter
    just consumes that stream -- TPMOptions.tokenizer_url was DELETED from
    the SDK, so tpm's own options must never mention it even when both a
    tokenizer and an enricher are composed in the same call as the tpm
    policy that (transitively) depends on the enricher's output."""
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER,
        enricher={"kubeconfig_path": "placeholder", "event_backbone_url": "nats://nats.platform.svc:4222"},
        policy=TPM_POLICY_ARGS,
        **ENRICHER_DEPS,
    )
    assert result["valid"] is True
    namespace = run_script(result["script"])

    assert namespace["enricher"].options.tokenizer_url == namespace["tokenizer"].endpoint
    assert not hasattr(namespace["policy"].options, "tokenizer_url")
    # The wiring appears exactly once -- into the enricher, not the policy.
    assert result["script"].count("tokenizer_url=stack.get('tokenizer_url')") == 1


def test_controlplane_requires_a_database_and_a_cache():
    result = build_full_stack_plan(cluster=BASE_CLUSTER, controlplane=CONTROLPLANE_ADMIN_ARGS)
    assert result["valid"] is False
    assert "database" in result["error"]
    assert "cache" in result["error"]


def test_controlplane_rejects_cnpg_without_a_database():
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, cnpg=CNPG_ARGS, valkey=VALKEY_ARGS, controlplane=CONTROLPLANE_ADMIN_ARGS,
    )
    assert result["valid"] is False


def test_controlplane_with_database_and_cache_is_valid():
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, cnpg=CNPG_WITH_DB_ARGS, valkey=VALKEY_ARGS, controlplane=CONTROLPLANE_ADMIN_ARGS,
    )
    assert result["valid"] is True
    assert result["layers"] == ["rke2", "valkey", "cnpg", "controlplane"]
    assert result["controlplane_endpoint"] == "http://admin-control-plane.control-plane.svc.cluster.local:8000"
    compile(result["script"], "<rendered>", "exec")


def test_controlplane_existing_secret_is_never_a_placeholder():
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, cnpg=CNPG_WITH_DB_ARGS, valkey=VALKEY_ARGS,
        controlplane={**CONTROLPLANE_ADMIN_ARGS, "kubeconfig_path": "SENTINEL-CP-KC"},
    )
    assert result["valid"] is True
    assert "SENTINEL-CP-KC" not in result["script"]
    assert "admin-cp-secrets" in result["script"]


def test_portal_without_its_control_plane_is_rejected():
    """A portal is the UI for its control plane's API."""
    result = build_full_stack_plan(cluster=BASE_CLUSTER, portal=PORTAL_ADMIN_ARGS)
    assert result["valid"] is False
    assert "needs a 'admin' controlplane" in result["error"]


def test_portal_rejects_empty_api_upstream_without_allow_no_api():
    result = build_full_stack_plan(cluster=BASE_CLUSTER, portal={"kubeconfig_path": "placeholder", "type": "admin"})
    assert result["valid"] is False
    assert "api_upstream is empty" in result["error"]


def test_portal_rejects_type_mismatch_with_controlplane():
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER,
        cnpg=CNPG_WITH_DB_ARGS,
        valkey=VALKEY_ARGS,
        controlplane=CONTROLPLANE_ADMIN_ARGS,
        portal={**PORTAL_ADMIN_ARGS, "type": "organization"},
    )
    assert result["valid"] is False
    assert "does not match" in result["error"]


def test_portal_wires_api_upstream_from_matching_controlplane():
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, cnpg=CNPG_WITH_DB_ARGS, valkey=VALKEY_ARGS,
        controlplane=CONTROLPLANE_ADMIN_ARGS,
        # A distinct, obviously-wrong sentinel: proves this is discarded
        # regardless of what the caller passed, since a real controlplane
        # is composed in the same call.
        portal={**PORTAL_ADMIN_ARGS, "api_upstream": "SENTINEL-API-UPSTREAM"},
    )
    assert result["valid"] is True
    assert result["layers"] == ["rke2", "valkey", "cnpg", "controlplane", "portal"]
    assert "SENTINEL-API-UPSTREAM" not in result["script"]
    assert "api_upstream=" not in result["script"].split("portal = stack.build(")[1].split(")\n")[0]
    compile(result["script"], "<rendered>", "exec")


def test_portal_controlplane_wiring_actually_executes_correctly():
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, cnpg=CNPG_WITH_DB_ARGS, valkey=VALKEY_ARGS,
        controlplane=CONTROLPLANE_ADMIN_ARGS, portal=PORTAL_ADMIN_ARGS,
    )
    assert result["valid"] is True

    with patch.object(RKE2Backend, "create", return_value="/tmp/fake-kubeconfig.yaml"), \
         patch.object(CacheBackend, "create", return_value=None), \
         patch.object(DatabaseBackend, "create", return_value=None), \
         patch.object(DatabaseBackend, "create_cluster", return_value=None), \
         patch.object(ControlPlaneBackend, "create", return_value=None), \
         patch.object(PortalBackend, "create", return_value=None):
        namespace = {}
        exec(compile(result["script"], "<generated>", "exec"), namespace)

    assert namespace["portal"].api_upstream == namespace["control_plane"].endpoint
    assert namespace["stack"].outputs["portal_endpoint"] == namespace["portal"].endpoint


# -- multiple instances of one capability -----------------------------------
# The real platform runs rpm AND tpm, both control planes, and both portals
# (see the API layer's communication diagram, and multistack/stack.py, which
# calls them "two instances of one capability"). Until these landed, that
# topology could not be expressed in a single script at all.

def test_both_rate_limiters_compose_together():
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER,
        enricher={"kubeconfig_path": "placeholder", "event_backbone_url": "nats://nats.platform.svc:4222"},
        policy=[{**POLICY_ARGS, "type": "rpm"}, {**POLICY_ARGS, "type": "tpm"}],
        **ENRICHER_DEPS,
    )
    assert result["valid"] is True
    assert result["layers"].index("policy:rpm") < result["layers"].index("gateway")
    assert "policy:tpm" in result["layers"]
    assert result["policy_endpoints"] == {
        "rpm": "http://rpm-rate-limiter-rpm.policy.svc:8000/check",
        "tpm": "http://tpm-rate-limiter-tpm.policy.svc:8000/check",
    }
    # Distinct variables, or the second would overwrite the first.
    assert "policy_rpm = stack.build(" in result["script"]
    assert "policy_tpm = stack.build(" in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_neither_limiter_gets_a_tokenizer_wired_directly():
    """The tokenizer's consumer is the enricher, not either limiter --
    confirmed by looking inside each Policy(...) call specifically, since
    a composed tokenizer + enricher together DOES put tokenizer_url
    somewhere in the script now (in the enricher's own options)."""
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER,
        enricher={"kubeconfig_path": "placeholder", "event_backbone_url": "nats://nats.platform.svc:4222"},
        policy=[{**POLICY_ARGS, "type": "rpm"}, {**POLICY_ARGS, "type": "tpm"}],
        **ENRICHER_DEPS,
    )
    assert result["valid"] is True
    for block in result["script"].split("stack.build(\n    Policy,")[1:]:
        policy_call = block.split("policy_backend = PolicyBackend()")[0]
        assert "tokenizer_url" not in policy_call
    compile(result["script"], "<rendered>", "exec")


def test_two_instances_of_the_same_type_are_rejected():
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER,
        policy=[{**POLICY_ARGS, "type": "rpm"}, {**POLICY_ARGS, "type": "rpm"}],
    )
    assert result["valid"] is False
    assert "same type" in result["error"]


def test_both_control_planes_and_both_portals_compose_together():
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER,
        valkey=VALKEY_ARGS,
        cnpg=CNPG_WITH_DB_ARGS,
        controlplane=[
            {**CONTROLPLANE_ADMIN_ARGS, "type": "admin"},
            {**CONTROLPLANE_ADMIN_ARGS, "type": "organization", "existing_secret": "org-cp-secrets"},
        ],
        portal=[
            {**PORTAL_ADMIN_ARGS, "type": "admin",
             "api_upstream": "http://admin-control-plane.control-plane.svc.cluster.local:8000"},
            {**PORTAL_ADMIN_ARGS, "type": "organization",
             "api_upstream": "http://organization-control-plane.control-plane.svc.cluster.local:8000"},
        ],
    )
    assert result["valid"] is True
    assert "controlplane:admin" in result["layers"] and "controlplane:organization" in result["layers"]
    assert "portal:admin" in result["layers"] and "portal:organization" in result["layers"]
    compile(result["script"], "<rendered>", "exec")


def test_each_portal_keeps_its_own_upstream_when_both_control_planes_exist():
    """Stack records only the LAST control plane, so its published
    controlplane_endpoint cannot be trusted to be this portal's -- the SDK
    says so itself. With two composed, each portal carries its own."""
    admin_ep = "http://admin-control-plane.control-plane.svc.cluster.local:8000"
    org_ep = "http://organization-control-plane.control-plane.svc.cluster.local:8000"
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, valkey=VALKEY_ARGS, cnpg=CNPG_WITH_DB_ARGS,
        controlplane=[
            {**CONTROLPLANE_ADMIN_ARGS, "type": "admin"},
            {**CONTROLPLANE_ADMIN_ARGS, "type": "organization", "existing_secret": "org-cp-secrets"},
        ],
        portal=[
            {**PORTAL_ADMIN_ARGS, "type": "admin", "api_upstream": admin_ep},
            {**PORTAL_ADMIN_ARGS, "type": "organization", "api_upstream": org_ep},
        ],
    )
    assert result["valid"] is True

    with patch.object(RKE2Backend, "create", return_value="/tmp/fake-kubeconfig.yaml"), \
         patch.object(CacheBackend, "create", return_value=None), \
         patch.object(DatabaseBackend, "create", return_value=None), \
         patch.object(DatabaseBackend, "create_cluster", return_value=None), \
         patch.object(ControlPlaneBackend, "create", return_value=None), \
         patch.object(PortalBackend, "create", return_value=None):
        namespace = {}
        exec(compile(result["script"], "<generated>", "exec"), namespace)

    assert namespace["portal_admin"].api_upstream == namespace["control_plane_admin"].endpoint
    assert namespace["portal_organization"].api_upstream == namespace["control_plane_organization"].endpoint


def test_a_portal_with_no_upstream_is_refused_when_the_target_is_ambiguous():
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, valkey=VALKEY_ARGS, cnpg=CNPG_WITH_DB_ARGS,
        controlplane=[
            {**CONTROLPLANE_ADMIN_ARGS, "type": "admin"},
            {**CONTROLPLANE_ADMIN_ARGS, "type": "organization", "existing_secret": "org-cp-secrets"},
        ],
        portal=[{"kubeconfig_path": "placeholder", "type": "admin", "allow_no_api": True}],
    )
    assert result["valid"] is False
    assert "explicit api_upstream" in result["error"]


def test_a_single_instance_still_behaves_exactly_as_before():
    """Passing one spec rather than a list must not change anything --
    same layer names, same endpoint keys, same auto-wiring."""
    result = build_full_stack_plan(cluster=BASE_CLUSTER, policy=POLICY_ARGS, **POLICY_DEPS)
    assert "policy" in result["layers"]
    assert "policy_endpoint" in result and "policy_endpoints" not in result
    assert "policy = stack.build(" in result["script"]


def test_composed_policy_renders_a_real_cache_auth_url_not_the_mask():
    """Same SecretStr masking bug as the standalone tool, in the composed
    renderer. cache_url is still stack-wired from the composed cache;
    cache_auth_url is a credential nothing here creates, so it must
    survive verbatim."""
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER,
        policy={**POLICY_ARGS, "cache_url": "placeholder",
                "cache_auth_url": "redis://:s3cret@platform-cache.valkey.svc:6379/0"},
        **POLICY_DEPS,
    )
    assert result["valid"] is True
    assert "cache_auth_url='redis://:s3cret@platform-cache.valkey.svc:6379/0'" in result["script"]
    assert "**********" not in result["script"]
    # cache_url itself is still the stack-wired one, not the placeholder.
    assert "cache_url=" not in result["script"].split("Policy,")[1].split(")")[0]
    compile(result["script"], "<rendered>", "exec")


# -- the four capabilities added after the ADR-030 merge ---------------------
NATS_URL = "nats://nats.platform.svc:4222"
ENRICHER_ARGS = {"kubeconfig_path": "placeholder", "event_backbone_url": NATS_URL}
BILLING_ARGS = {"kubeconfig_path": "placeholder", "existing_secret": "billing-secrets",
                "event_backbone_url": NATS_URL}
ROUTE_ARGS = {"kubeconfig_path": "placeholder", "name": "gw-route", "namespace": "gateway",
              "service": "gateway-model-gateway", "port": 8080, "path_prefix": "/v1"}
ACCELERATOR_ARGS = {"kubeconfig_path": "placeholder"}


def test_the_tokenizer_now_wires_into_the_enricher():
    """ADR-030 moved token counting out of the tpm limiter and into the
    enricher, so this is where the old tokenizer wiring went."""
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, enricher=ENRICHER_ARGS, **ENRICHER_DEPS,
    )
    assert result["valid"] is True
    assert "tokenizer_url=stack.get('tokenizer_url')" in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_enricher_alone_is_rejected_naming_what_it_consumes():
    """It consumes the gateway's raw events off NATS -- without the
    gateway nothing is ever published."""
    result = build_full_stack_plan(cluster=BASE_CLUSTER, enricher=ENRICHER_ARGS)
    assert result["valid"] is False
    assert "enricher needs gateway" in result["error"]


def test_enricher_without_its_tokenizer_is_rejected():
    deps = {k: v for k, v in ENRICHER_DEPS.items() if k != "tokenizer"}
    result = build_full_stack_plan(cluster=BASE_CLUSTER, enricher=ENRICHER_ARGS, **deps)
    assert result["valid"] is False
    assert "enricher needs tokenizer" in result["error"]


def test_billing_requires_a_real_database():
    result = build_full_stack_plan(cluster=BASE_CLUSTER, billing=BILLING_ARGS)
    assert result["valid"] is False
    assert "database" in result["error"]


def test_billing_requires_an_enricher():
    """Billing's consumer drains ONLY the enricher's derived
    gateway.events.enriched stream -- nothing else produces it. Without
    one, billing is deployed, healthy, and meters nothing."""
    result = build_full_stack_plan(cluster=BASE_CLUSTER, cnpg=CNPG_WITH_DB_ARGS, billing=BILLING_ARGS)
    assert result["valid"] is False
    assert "enricher" in result["error"]


def test_tpm_policy_requires_an_enricher():
    """A type="tpm" policy reads gateway.events.enriched by default
    (TPMOptions.event_stream_subject) -- it does not compute token counts
    itself. Without an enricher composed, it is deployed, healthy, and
    silently allows every request."""
    result = build_full_stack_plan(cluster=BASE_CLUSTER, policy=TPM_POLICY_ARGS)
    assert result["valid"] is False
    assert "enricher" in result["error"]


def test_rpm_policy_does_not_require_an_enricher():
    """rpm reads the RAW gateway.events stream directly -- it has no
    dependency on the enricher."""
    result = build_full_stack_plan(cluster=BASE_CLUSTER, policy=POLICY_ARGS, **POLICY_DEPS)
    assert result["valid"] is True


def test_tpm_policy_pointed_at_an_explicit_non_enriched_subject_does_not_require_an_enricher():
    """TPMOptions.event_stream_subject is a real, driver-honored override
    (see multistack/policy/drivers/tpm.py) -- a caller who has
    deliberately pointed it elsewhere (an existing enricher on another
    cluster, a custom subject) is not missing anything, and this tool
    must not invent a requirement the SDK itself doesn't have."""
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER,
        policy={**TPM_POLICY_ARGS, "options": {"event_stream_subject": "gateway.events"}},
        **POLICY_DEPS,
    )
    assert result["valid"] is True


def test_billing_with_a_database_composes():
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, enricher=ENRICHER_ARGS, billing=BILLING_ARGS, **ENRICHER_DEPS,
    )
    assert result["valid"] is True
    assert "billing" in result["layers"]
    assert result["billing_endpoint"] == "http://billing.billing.svc.cluster.local:8000"
    assert "billing-secrets" in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_route_requires_an_ingress_gateway():
    """A route goes THROUGH the front door; it does not create one."""
    result = build_full_stack_plan(cluster=BASE_CLUSTER, route=ROUTE_ARGS)
    assert result["valid"] is False
    assert "ingress" in result["error"]


def test_route_with_an_ingress_wires_its_address_from_that_layer():
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, ingress=INGRESS_ARGS, route=ROUTE_ARGS,
    )
    assert result["valid"] is True
    assert "route:gw-route" in result["layers"]
    # ingress_address is a placeholder -- MetalLB assigns it at run time.
    route_block = result["script"].split("    Route,")[1].split(")")[0]
    assert "ingress_address=" not in route_block
    compile(result["script"], "<rendered>", "exec")


def test_several_routes_get_distinct_variables():
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, ingress=INGRESS_ARGS,
        route=[ROUTE_ARGS, {**ROUTE_ARGS, "name": "portal-route", "service": "admin-portal", "port": 80}],
    )
    assert result["valid"] is True
    assert "route_gw_route = stack.build(" in result["script"]
    assert "route_portal_route = stack.build(" in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_two_routes_with_the_same_name_are_rejected():
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, ingress=INGRESS_ARGS, route=[ROUTE_ARGS, ROUTE_ARGS],
    )
    assert result["valid"] is False
    assert "same name" in result["error"]


def test_accelerator_is_rendered_before_inference():
    """It makes nvidia.com/gpu a resource a pod can request; an Inference
    created first would sit Pending until it existed."""
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, accelerator=ACCELERATOR_ARGS, inference=INFERENCE_ARGS,
    )
    assert result["valid"] is True
    assert result["script"].index("Accelerator,") < result["script"].index("Inference,")
    compile(result["script"], "<rendered>", "exec")


# ---- wiring for the admin_cp_url / billing_internal_url / gateway_upstream fields

from multistack import Billing, ControlPlane, Gateway

PORTAL_ORG_ARGS = {
    "kubeconfig_path": "placeholder",
    "type": "organization",
    "api_upstream": "http://organization-control-plane.platform.svc:8000",
}
ADMIN_CP_ENDPOINT = ControlPlane(**CONTROLPLANE_ADMIN_ARGS).endpoint


def test_policy_admin_cp_url_is_wired_from_the_admin_control_plane():
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER,
        policy={**POLICY_ARGS, "admin_cp_service_api_key": "admin-key"},
        **{**POLICY_DEPS, "controlplane": [CONTROLPLANE_ADMIN_ARGS, CONTROLPLANE_ORG_ARGS]},
    )
    assert result["valid"] is True, result
    assert f"admin_cp_url={ADMIN_CP_ENDPOINT!r}," in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_policy_admin_cp_url_is_not_wired_without_the_key():
    """Wiring the URL alone would opt the caller into a lookup that 401s
    and silently falls back -- the static limits are what they asked for."""
    result = build_full_stack_plan(cluster=BASE_CLUSTER, policy=POLICY_ARGS, **{**POLICY_DEPS, "controlplane": [CONTROLPLANE_ADMIN_ARGS, CONTROLPLANE_ORG_ARGS]})
    assert result["valid"] is True, result
    assert "admin_cp_url=''," in result["script"]


def test_policy_explicit_admin_cp_url_is_not_overridden():
    explicit = "http://admin-cp.elsewhere.svc:8000"
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER,
        policy={**POLICY_ARGS, "admin_cp_url": explicit, "admin_cp_service_api_key": "admin-key"},
        **{**POLICY_DEPS, "controlplane": [CONTROLPLANE_ADMIN_ARGS, CONTROLPLANE_ORG_ARGS]},
    )
    assert result["valid"] is True, result
    assert f"admin_cp_url={explicit!r}," in result["script"]
    assert ADMIN_CP_ENDPOINT not in result["script"]


def test_policy_admin_cp_url_without_its_key_is_rejected():
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, policy={**POLICY_ARGS, "admin_cp_url": ADMIN_CP_ENDPOINT},
    )
    assert result["valid"] is False
    assert "admin_cp_service_api_key" in result["error"]


def test_every_control_plane_gets_billing_internal_url_from_billing():
    billing_endpoint = Billing(**BILLING_ARGS).endpoint
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, billing=BILLING_ARGS, enricher=ENRICHER_ARGS,
        **{**ENRICHER_DEPS, "controlplane": [CONTROLPLANE_ADMIN_ARGS, CONTROLPLANE_ORG_ARGS]},
    )
    assert result["valid"] is True, result
    assert result["script"].count(f"billing_internal_url={billing_endpoint!r},") == 2
    compile(result["script"], "<rendered>", "exec")


def test_control_plane_explicit_billing_internal_url_is_not_overridden():
    explicit = "http://billing.elsewhere.svc:8000"
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, billing=BILLING_ARGS, enricher=ENRICHER_ARGS,
        **{**ENRICHER_DEPS, "controlplane": [
            {**CONTROLPLANE_ADMIN_ARGS, "billing_internal_url": explicit}, CONTROLPLANE_ORG_ARGS,
        ]},
    )
    assert result["valid"] is True, result
    assert f"billing_internal_url={explicit!r}," in result["script"]
    # Only the organization control plane, which left it unset, is wired.
    assert result["script"].count(f"billing_internal_url={Billing(**BILLING_ARGS).endpoint!r},") == 1


def test_control_plane_billing_internal_url_stays_unset_without_billing():
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, cnpg=CNPG_WITH_DB_ARGS, valkey=VALKEY_ARGS,
        controlplane=CONTROLPLANE_ADMIN_ARGS,
    )
    assert result["valid"] is True, result
    assert "billing_internal_url=None," in result["script"]


def test_organization_portal_gets_gateway_upstream_from_the_gateway():
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, gateway=GATEWAY_ARGS, portal=PORTAL_ORG_ARGS,
        **GATEWAY_BASELINE_KWARGS,
    )
    assert result["valid"] is True, result
    script = result["script"]
    assert "    gateway_upstream=gateway.endpoint," in script
    # Only valid if the gateway variable exists by the time the portal is built.
    assert script.index("gateway = stack.build(") < script.index("gateway_upstream=gateway.endpoint")
    compile(script, "<rendered>", "exec")


def test_admin_portal_gets_no_gateway_upstream():
    """The admin portal has no chat, so there is nothing to proxy /mg to."""
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, gateway=GATEWAY_ARGS, cnpg=CNPG_WITH_DB_ARGS, valkey=VALKEY_ARGS,
        controlplane=[CONTROLPLANE_ORG_ARGS, CONTROLPLANE_ADMIN_ARGS], portal=PORTAL_ADMIN_ARGS,
    )
    assert result["valid"] is True, result
    assert "gateway_upstream=gateway.endpoint" not in result["script"]
    assert "gateway_upstream=''," in result["script"]


def test_portal_explicit_gateway_upstream_is_not_overridden():
    explicit = "http://gateway.elsewhere.svc:8080"
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, gateway=GATEWAY_ARGS,
        portal={**PORTAL_ORG_ARGS, "gateway_upstream": explicit},
        **GATEWAY_BASELINE_KWARGS,
    )
    assert result["valid"] is True, result
    assert f"gateway_upstream={explicit!r}," in result["script"]
    assert "gateway_upstream=gateway.endpoint" not in result["script"]


# ---- existing clusters, the event backbone, and the dependency chain

EXISTING = {"kubeconfig_path": "/tmp/kubeconfig", "existing_storage_class": "longhorn"}
TPM_CHAIN_KWARGS = {
    **{k: v for k, v in ENRICHER_DEPS.items() if k != "storage"},
    "enricher": ENRICHER_ARGS,
    "policy": {**TPM_POLICY_ARGS, "allow_no_backbone": False, "event_backbone_url": NATS_URL},
}


def test_an_existing_cluster_plan_never_provisions_a_cluster():
    """The reason the whole-stack tool was once kept away from existing
    clusters: it always built one. With kubeconfig_path it builds nothing
    of the kind -- checked by running the script, not by reading it."""
    result = build_full_stack_plan(**EXISTING, **TPM_CHAIN_KWARGS)
    assert result["valid"] is True, result
    assert "RKE2" not in result["script"]
    assert result["kubeconfig_path"] == "/tmp/kubeconfig" and "cluster_name" not in result

    with patch.object(RKE2Backend, "create") as create_cluster:
        namespace = run_script(result["script"])
    create_cluster.assert_not_called()
    assert namespace["stack"].outputs["kubeconfig_path"] == "/tmp/kubeconfig"
    assert namespace["queue"].storage_class == "longhorn"


def test_the_user_request_that_started_this_builds_the_tpm_chain():
    """Rate limiting by tokens on an existing cluster: the architecture's
    own sequence, every piece present and wired, in dependency order."""
    result = build_full_stack_plan(**EXISTING, **TPM_CHAIN_KWARGS)
    assert result["valid"] is True, result
    layers = result["layers"]
    assert {"cnpg", "valkey", "controlplane", "gateway", "queue", "tokenizer", "enricher", "policy"} <= set(layers)
    for first, then in [("controlplane", "gateway"), ("queue", "enricher"), ("tokenizer", "enricher"), ("cnpg", "controlplane")]:
        assert layers.index(first) < layers.index(then), (first, then, layers)


def test_the_queue_is_wired_into_every_event_consumer_at_runtime():
    result = build_full_stack_plan(
        cluster=BASE_CLUSTER, billing=BILLING_ARGS, **TPM_CHAIN_KWARGS, storage=STORAGE_ARGS,
    )
    assert result["valid"] is True, result
    namespace = run_script(result["script"])
    backbone = namespace["queue"].endpoint
    assert result["event_backbone_url"] == backbone
    for name in ("gateway", "policy", "enricher", "billing"):
        assert namespace[name].event_backbone_url == backbone, name


def test_the_gateway_verifies_keys_against_the_composed_org_control_plane():
    result = build_full_stack_plan(cluster=BASE_CLUSTER, **REQUEST_PATH_KWARGS)
    assert result["valid"] is True, result
    namespace = run_script(result["script"])
    assert namespace["gateway"].org_cp_internal_url == namespace["control_plane"].endpoint


def test_the_org_control_plane_is_built_before_the_gateway():
    result = build_full_stack_plan(cluster=BASE_CLUSTER, **REQUEST_PATH_KWARGS)
    script = result["script"]
    assert script.index("ControlPlane,") < script.index("Gateway,")


def test_exactly_one_target_cluster_is_required():
    for kwargs in ({}, {"cluster": BASE_CLUSTER, "kubeconfig_path": "/tmp/kubeconfig"}):
        result = build_full_stack_plan(valkey=VALKEY_ARGS, **kwargs)
        assert result["valid"] is False
        assert "exactly one of `cluster`" in result["error"]


def test_storage_cannot_be_composed_onto_an_existing_cluster():
    """Installing Longhorn needs the node list only a new cluster has here."""
    result = build_full_stack_plan(kubeconfig_path="/tmp/kubeconfig", storage=STORAGE_ARGS)
    assert result["valid"] is False
    assert "existing_storage_class" in result["error"]


def test_the_queue_needs_storage_on_an_existing_cluster_too():
    result = build_full_stack_plan(kubeconfig_path="/tmp/kubeconfig", queue=QUEUE_ARGS)
    assert result["valid"] is False
    assert "queue needs storage" in result["error"]


def test_a_rate_limiter_without_the_queue_is_rejected_unless_it_opts_out():
    rpm = {**POLICY_ARGS, "allow_no_backbone": False, "event_backbone_url": NATS_URL}
    result = build_full_stack_plan(cluster=BASE_CLUSTER, policy=rpm, **POLICY_DEPS)
    assert result["valid"] is False
    assert "policy needs queue" in result["error"]
    # The SDK's own explicit opt-out still works.
    assert build_full_stack_plan(cluster=BASE_CLUSTER, policy=POLICY_ARGS, **POLICY_DEPS)["valid"] is True


def test_a_rejection_names_the_whole_missing_chain_and_only_the_real_values():
    """Told only the first gap, the model found the chain one rejection at a
    time, asked whether to include it at all, and asked about fields that
    have defaults."""
    error = build_full_stack_plan(kubeconfig_path="/tmp/kubeconfig", tokenizer=TOKENIZER_ARGS)["error"]
    for component in ("enricher", "gateway", "queue", "cnpg", "valkey", "controlplane"):
        assert component in error
    assert "do not ask the user whether to include them" in error
    assert "existing_secret" in error and "database.password" in error
    assert "max_deliver" not in error


def test_prepare_args_fills_only_what_this_tool_wires():
    from mcp_tools.full_stack import PLACEHOLDER, prepare_args

    args = prepare_args({
        "kubeconfig_path": "/tmp/kubeconfig", "existing_storage_class": "longhorn",
        "cnpg": {"name": "org-db", "database": {"name": "org", "owner": "org", "password": "s3cret"}},
        "valkey": {"name": "platform-cache"},
        "controlplane": {"type": "organization", "existing_secret": "org-cp-secrets"},
        "gateway": {"api_key_secret": "gateway-keys", "upstream_url": "http://vllm.inference.svc:8000"},
        "queue": {}, "enricher": {}, "tokenizer": {},
    })
    assert args["gateway"]["org_cp_internal_url"] == PLACEHOLDER
    assert args["enricher"]["event_backbone_url"] == PLACEHOLDER
    # An explicit value is never replaced, and real values are never invented.
    assert args["gateway"]["upstream_url"] == "http://vllm.inference.svc:8000"
    assert "existing_secret" not in args.get("billing", {})
    assert build_full_stack_plan(**args)["valid"] is True


def test_prepare_args_does_not_invent_an_upstream_without_inference():
    from mcp_tools.full_stack import prepare_args

    args = prepare_args({"controlplane": {"type": "organization"}, "gateway": {"api_key_secret": "k"}})
    assert "upstream_url" not in args["gateway"]
