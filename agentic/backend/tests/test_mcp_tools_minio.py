"""Tests for the MinIO MCP tool (agentic/backend/src/mcp_tools/minio.py). Calls
the tool function directly as plain Python -- no MCP transport
involved, no real cluster touched (MinIOBackend is never called)."""

import pytest
from pydantic import ValidationError

from mcp_tools.minio import build_minio_plan


def test_build_valid_plan_includes_every_field():
    result = build_minio_plan(
        tenant={"kubeconfig_path": "/tmp/kc.yaml", "name": "minio-test", "servers": 2, "volumes_per_server": 2}
    )
    assert result["valid"] is True
    assert result["name"] == "minio-test"
    assert result["endpoint"] == "https://minio.minio.svc.cluster.local"
    assert "MinIOTenant(" in result["script"]
    assert "backend.create(tenant)" in result["script"]
    compile(result["script"], "<rendered>", "exec")  # syntax check


def test_build_rejects_too_few_drives_for_distributed_mode():
    # MIN_DISTRIBUTED_DRIVES is 4; 1 server x 1 volume = 1 drive.
    with pytest.raises(ValidationError, match="erasure coding"):
        build_minio_plan(
            tenant={
                "kubeconfig_path": "/tmp/kc.yaml",
                "name": "too-small",
                "mode": "distributed",
                "servers": 1,
                "volumes_per_server": 1,
            }
        )


def test_build_rejects_mismatched_root_credentials():
    # root_user/root_password must be set together or both left unset.
    with pytest.raises(ValidationError, match="together"):
        build_minio_plan(
            tenant={"kubeconfig_path": "/tmp/kc.yaml", "name": "x", "root_user": "admin"}
        )


def test_build_leaves_credentials_unset_for_real_generation():
    # BASE_INSTRUCTIONS tells the model never to invent a plausible-looking
    # value for something meant to be generated -- this is what "leave it
    # unset" looks like structurally: root_user/root_password stay None in
    # the rendered script, for MinIOTenant.ensure_credentials() to fill at
    # run time, not something the model guessed at plan time.
    result = build_minio_plan(tenant={"kubeconfig_path": "/tmp/kc.yaml", "name": "x"})
    assert result["valid"] is True
    assert "root_user=None" in result["script"]
    assert "root_password=None" in result["script"]
