"""Tests for the ControlPlane MCP tool
(agentic/backend/src/mcp_tools/controlplane.py). Calls the tool function
directly as plain Python -- no MCP transport involved, no real cluster
touched (ControlPlaneBackend is never called)."""

import pytest
from pydantic import ValidationError

from mcp_tools.controlplane import build_controlplane_plan


def test_build_valid_admin_plan():
    result = build_controlplane_plan(control_plane={
        "kubeconfig_path": "/tmp/kc.yaml",
        "type": "admin",
        "existing_secret": "admin-cp-secrets",
    })
    assert result["valid"] is True
    assert result["endpoint"] == "http://admin-control-plane.control-plane.svc.cluster.local:8000"
    assert "ControlPlane(" in result["script"]
    assert "backend.create(control_plane)" in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_build_valid_organization_plan():
    result = build_controlplane_plan(control_plane={
        "kubeconfig_path": "/tmp/kc.yaml",
        "type": "organization",
        "existing_secret": "org-cp-secrets",
    })
    assert result["valid"] is True
    assert result["endpoint"] == "http://organization-control-plane.control-plane.svc.cluster.local:8000"
    assert "OrganizationControlPlaneOptions(" in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_build_rejects_missing_existing_secret():
    with pytest.raises(ValidationError, match="existing_secret"):
        build_controlplane_plan(control_plane={
            "kubeconfig_path": "/tmp/kc.yaml",
            "type": "admin",
        })


def test_build_rejects_missing_kubeconfig_path():
    with pytest.raises(ValidationError):
        build_controlplane_plan(control_plane={"type": "admin", "existing_secret": "s"})


def test_build_never_appears_to_validate_the_secret_itself():
    # This tool must never reject or "improve" a Secret name -- whatever
    # real name the user gave travels through unchanged, since this tool
    # never reads or writes the Secret's actual contents.
    result = build_controlplane_plan(control_plane={
        "kubeconfig_path": "/tmp/kc.yaml",
        "type": "admin",
        "existing_secret": "whatever-the-user-named-it",
    })
    assert result["valid"] is True
    assert "whatever-the-user-named-it" in result["script"]


def test_build_script_includes_every_field_the_controlplane_actually_has():
    result = build_controlplane_plan(control_plane={
        "kubeconfig_path": "/tmp/kc.yaml",
        "type": "admin",
        "existing_secret": "admin-cp-secrets",
        "replicas": 5,
        "peer_url": "http://organization-control-plane.platform.svc:8000",
    })
    assert result["valid"] is True
    assert "replicas=5" in result["script"]
    assert "peer_url='http://organization-control-plane.platform.svc:8000'" in result["script"]
