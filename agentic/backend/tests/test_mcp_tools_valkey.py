"""Tests for the Valkey MCP tool (agentic/backend/src/mcp_tools/valkey.py). Calls
the tool function directly as plain Python -- no MCP transport
involved, no real cluster touched (CacheBackend is never called)."""

import pytest
from pydantic import ValidationError

from mcp_tools.valkey import build_valkey_plan


def test_build_valid_plan_with_defaults():
    result = build_valkey_plan(valkey={"kubeconfig_path": "/tmp/kc.yaml", "name": "platform-cache"})
    assert result["valid"] is True
    assert result["name"] == "platform-cache"
    assert result["namespace"] == "valkey"
    assert "Cache(" in result["script"]
    assert "backend.create(cache)" in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_build_valid_plan_with_custom_values():
    # chart/chart_version/values moved under `options` when the
    # capability left the core/ + backends/ split: they are Bitnami-chart
    # specifics, and a second cache implementation would not have them.
    result = build_valkey_plan(valkey={
        "kubeconfig_path": "/tmp/kc.yaml",
        "name": "platform-cache",
        "namespace": "custom-ns",
        "options": {
            "chart_version": "1.2.3",
            "values": {"auth": {"enabled": False}},
        },
    })
    assert result["valid"] is True
    assert "namespace='custom-ns'" in result["script"]
    assert "chart_version='1.2.3'" in result["script"]
    assert "values={'auth': {'enabled': False}}" in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_build_rejects_missing_kubeconfig_path():
    with pytest.raises(ValidationError):
        build_valkey_plan(valkey={"name": "platform-cache"})


def test_build_rejects_missing_name():
    with pytest.raises(ValidationError):
        build_valkey_plan(valkey={"kubeconfig_path": "/tmp/kc.yaml"})


def test_build_returns_the_real_computed_endpoint():
    # Cache.PROVIDES = {"cache_url": "endpoint"} now -- confirm the
    # tool surfaces the real, deterministic endpoint (same reasoning as
    # build_gateway_plan/build_policy_plan/build_inference_plan), and
    # that the script itself never states a redis:// value (the real
    # script only constructs the Cache object and calls create() --
    # the endpoint is computed by the SDK, not embedded as a literal).
    result = build_valkey_plan(valkey={"kubeconfig_path": "/tmp/kc.yaml", "name": "platform-cache"})
    assert result["endpoint"] == "redis://platform-cache-valkey-primary.valkey.svc.cluster.local:6379/0"
    assert "redis://" not in result["script"]


def test_build_endpoint_accounts_for_bitnami_fullname_collapsing():
    # Regression guard for the exact risk raised before this landed:
    # a naive f"{name}.{namespace}" guess would be wrong here, since
    # Bitnami's fullname template collapses the release name into the
    # chart name when it already contains it.
    result = build_valkey_plan(valkey={"kubeconfig_path": "/tmp/kc.yaml", "name": "valkey-test"})
    assert result["endpoint"] == "redis://valkey-test-primary.valkey.svc.cluster.local:6379/0"


def test_build_script_includes_every_field_the_valkey_actually_has():
    result = build_valkey_plan(valkey={
        "kubeconfig_path": "/tmp/kc.yaml",
        "name": "platform-cache",
        "options": {"chart": "oci://registry.example.com/valkey"},
    })
    assert result["valid"] is True
    assert "chart='oci://registry.example.com/valkey'" in result["script"]
