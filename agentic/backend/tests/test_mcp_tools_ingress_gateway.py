"""Tests for the Ingress Gateway MCP tool
(agentic/backend/src/mcp_tools/ingress_gateway.py). Calls the tool function
directly as plain Python -- no MCP transport involved, no real cluster
touched (IngressGatewayBackend is never called)."""

import pytest
from pydantic import ValidationError

from mcp_tools.ingress_gateway import build_ingress_gateway_plan


def test_build_valid_plan_with_a_range():
    result = build_ingress_gateway_plan(gateway={
        "kubeconfig_path": "/tmp/kc.yaml",
        "address_pool": ["192.168.1.240-192.168.1.250"],
    })
    assert result["valid"] is True
    assert result["address_pool"] == ["192.168.1.240-192.168.1.250"]
    assert "IngressGateway(" in result["script"]
    assert "backend.create(gateway)" in result["script"]
    compile(result["script"], "<rendered>", "exec")  # syntax check


def test_build_valid_plan_with_a_cidr():
    result = build_ingress_gateway_plan(gateway={
        "kubeconfig_path": "/tmp/kc.yaml",
        "address_pool": ["192.168.1.0/24"],
    })
    assert result["valid"] is True
    compile(result["script"], "<rendered>", "exec")


def test_build_rejects_empty_address_pool():
    with pytest.raises(ValidationError, match="address_pool is empty"):
        build_ingress_gateway_plan(gateway={
            "kubeconfig_path": "/tmp/kc.yaml",
            "address_pool": [],
        })


def test_build_rejects_an_entry_that_is_not_a_range_or_cidr():
    with pytest.raises(ValidationError, match="not a MetalLB range"):
        build_ingress_gateway_plan(gateway={
            "kubeconfig_path": "/tmp/kc.yaml",
            "address_pool": ["justanaddress"],
        })


def test_build_rejects_missing_kubeconfig_path():
    with pytest.raises(ValidationError):
        build_ingress_gateway_plan(gateway={
            "address_pool": ["192.168.1.240-192.168.1.250"],
        })


def test_import_is_from_the_submodule_not_top_level():
    # IngressGateway/IngressGatewayBackend are NOT re-exported at the top
    # level `multistack` package (unlike Gateway/Policy/Storage) -- the
    # rendered script must import from multistack.ingress_gateway, or it
    # would fail at runtime with an ImportError the SDK's own top-level
    # __init__.py doesn't raise until the script actually executes.
    result = build_ingress_gateway_plan(gateway={
        "kubeconfig_path": "/tmp/kc.yaml",
        "address_pool": ["192.168.1.240-192.168.1.250"],
    })
    assert "from multistack.ingress_gateway import IngressGateway, IngressGatewayBackend" in result["script"]
    assert "from multistack import IngressGateway" not in result["script"]


def test_build_script_includes_options_when_set():
    result = build_ingress_gateway_plan(gateway={
        "kubeconfig_path": "/tmp/kc.yaml",
        "address_pool": ["192.168.1.240-192.168.1.250"],
        "options": {"metallb_namespace": "custom-metallb"},
    })
    assert result["valid"] is True
    assert "MetalLBIstioOptions(" in result["script"]
    assert "metallb_namespace='custom-metallb'" in result["script"]
    compile(result["script"], "<rendered>", "exec")
