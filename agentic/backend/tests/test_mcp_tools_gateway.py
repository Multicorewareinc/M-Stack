"""Tests for the Gateway MCP tool (agentic/backend/src/mcp_tools/gateway.py).
Calls the tool function directly as plain Python -- no MCP transport
involved, no real cluster touched (GatewayBackend is never called)."""

import pytest
from pydantic import ValidationError

from mcp_tools.gateway import build_gateway_plan


def test_build_valid_plan_is_a_plain_proxy_by_default():
    result = build_gateway_plan(gateway={
        "kubeconfig_path": "/tmp/kc.yaml",
        "upstream_url": "http://vllm-service.inference.svc:8000",
        "api_key_secret": "gateway-keys",
        "org_cp_internal_url": "http://organization-control-plane.control-plane.svc:8000",
    })
    assert result["valid"] is True
    assert result["upstream_url"] == "http://vllm-service.inference.svc:8000"
    assert "Gateway(" in result["script"]
    assert "backend.create(gateway)" in result["script"]
    compile(result["script"], "<rendered>", "exec")  # syntax check


def test_build_rejects_policy_timeout_too_low_for_a_configured_chain():
    with pytest.raises(ValidationError, match="policy_timeout_ms"):
        build_gateway_plan(gateway={
            "kubeconfig_path": "/tmp/kc.yaml",
            "upstream_url": "http://vllm-service.inference.svc:8000",
            "api_key_secret": "gateway-keys",
            "org_cp_internal_url": "http://organization-control-plane.control-plane.svc:8000",
            "policy_endpoints": ["http://rpm-rate-limiter-rpm.policy.svc:8000/check"],
            "policy_timeout_ms": 50,
        })


def test_build_rejects_a_model_route_carrying_credentials():
    # model_routes must be plain URLs -- a route needing its own api_key
    # belongs in the Secret named by api_key_secret, not the spec.
    with pytest.raises(ValidationError, match="Secret"):
        build_gateway_plan(gateway={
            "kubeconfig_path": "/tmp/kc.yaml",
            "upstream_url": "http://vllm-service.inference.svc:8000",
            "api_key_secret": "gateway-keys",
            "org_cp_internal_url": "http://organization-control-plane.control-plane.svc:8000",
            "model_routes": {"claude": {"url": "https://api.anthropic.com", "api_key": "sk-ant-..."}},
        })


def test_build_script_includes_every_field_the_gateway_actually_has():
    result = build_gateway_plan(gateway={
        "kubeconfig_path": "/tmp/kc.yaml",
        "upstream_url": "http://vllm-service.inference.svc:8000",
        "api_key_secret": "gateway-keys",
        "org_cp_internal_url": "http://organization-control-plane.control-plane.svc:8000",
        "model_routes": {"llama-3-8b": "http://vllm-alt.inference.svc:8000"},
        "replicas": 3,
    })
    assert result["valid"] is True
    assert "llama-3-8b" in result["script"]
    assert "replicas=3" in result["script"]
