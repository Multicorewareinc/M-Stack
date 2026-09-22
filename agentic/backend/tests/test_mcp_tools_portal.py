"""Tests for the Portal MCP tool (agentic/backend/src/mcp_tools/portal.py).
Calls the tool function directly as plain Python -- no MCP transport
involved, no real cluster touched (PortalBackend is never called)."""

import pytest
from pydantic import ValidationError

from mcp_tools.portal import build_portal_plan


def test_build_valid_admin_plan_with_api_upstream():
    result = build_portal_plan(portal={
        "kubeconfig_path": "/tmp/kc.yaml",
        "type": "admin",
        "api_upstream": "http://admin-control-plane.platform.svc:8000",
    })
    assert result["valid"] is True
    assert result["endpoint"] == "http://admin-portal.frontend.svc.cluster.local:80"
    assert "Portal(" in result["script"]
    assert "backend.create(portal)" in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_build_valid_organization_plan():
    result = build_portal_plan(portal={
        "kubeconfig_path": "/tmp/kc.yaml",
        "type": "organization",
        "api_upstream": "http://organization-control-plane.platform.svc:8000",
    })
    assert result["valid"] is True
    assert "OrganizationPortalOptions(" in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_build_rejects_empty_api_upstream_without_opt_out():
    # Regression test: Portal has no model_validator re-running its own
    # validate() at construction (unlike Gateway/Policy) -- confirms
    # build_portal_plan calls it explicitly rather than rendering a
    # script that would silently fall through to the SPA at runtime.
    result = build_portal_plan(portal={
        "kubeconfig_path": "/tmp/kc.yaml",
        "type": "organization",
    })
    assert result["valid"] is False
    assert "api_upstream is empty" in result["error"]


def test_build_allows_empty_api_upstream_with_explicit_opt_out():
    result = build_portal_plan(portal={
        "kubeconfig_path": "/tmp/kc.yaml",
        "type": "organization",
        "allow_no_api": True,
    })
    assert result["valid"] is True
    compile(result["script"], "<rendered>", "exec")


def test_build_rejects_missing_kubeconfig_path():
    with pytest.raises(ValidationError):
        build_portal_plan(portal={"type": "admin", "api_upstream": "http://x.platform.svc:8000"})


def test_build_rejects_hostname_resolver():
    with pytest.raises(ValidationError, match="resolver"):
        build_portal_plan(portal={
            "kubeconfig_path": "/tmp/kc.yaml",
            "type": "admin",
            "api_upstream": "http://admin-control-plane.platform.svc:8000",
            "resolver": "coredns.kube-system.svc",
        })
