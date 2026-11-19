"""Tests for the SeaweedFS MCP tool (agentic/backend/src/mcp_tools/seaweedfs.py).
Calls the tool function directly as plain Python -- no MCP transport
involved, no real cluster touched (SeaweedFSBackend is never called)."""

import pytest
from mcp_tools.seaweedfs import build_seaweedfs_plan
from pydantic import ValidationError


def test_build_valid_plan_includes_every_field():
    result = build_seaweedfs_plan(
        cluster={
            "kubeconfig_path": "/tmp/kc.yaml",
            "name": "weights",
            "volume_replicas": 2,
            "replication": "001",
        }
    )
    assert result["valid"] is True
    assert result["name"] == "weights" and result["replication"] == "001"
    assert result["endpoint"] == "http://weights-seaweedfs-s3.seaweedfs.svc.cluster.local:8333"
    assert "SeaweedFSCluster(" in result["script"]
    assert "backend.create(objectstore)" in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_build_rejects_an_invalid_replication_code():
    with pytest.raises(ValidationError, match="replication"):
        build_seaweedfs_plan(
            cluster={"kubeconfig_path": "/tmp/kc.yaml", "name": "x", "replication": "1"}
        )


def test_build_rejects_one_credential_without_the_other():
    with pytest.raises(ValidationError, match="together"):
        build_seaweedfs_plan(
            cluster={"kubeconfig_path": "/tmp/kc.yaml", "name": "x", "access_key": "k"}
        )


def test_build_rejects_a_volume_size_without_a_unit_it_cannot_parse():
    with pytest.raises(ValidationError):
        build_seaweedfs_plan(
            cluster={"kubeconfig_path": "/tmp/kc.yaml", "name": "x", "volume_size": "lots"}
        )


def test_build_leaves_credentials_unset_for_real_generation():
    result = build_seaweedfs_plan(cluster={"kubeconfig_path": "/tmp/kc.yaml", "name": "x"})
    assert result["valid"] is True
    assert "access_key=None" in result["script"] and "secret_key=None" in result["script"]
