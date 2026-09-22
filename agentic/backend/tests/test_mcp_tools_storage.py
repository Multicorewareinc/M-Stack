"""Tests for the Storage MCP tool (agentic/backend/src/mcp_tools/storage.py).
Calls the tool function directly as plain Python -- no MCP transport
involved, no real cluster touched (StorageBackend is never called)."""

import pytest
from pydantic import ValidationError

from mcp_tools.storage import build_storage_plan


def test_build_valid_plan_includes_options_and_nodes():
    result = build_storage_plan(
        storage={"type": "longhorn", "kubeconfig_path": "/tmp/kc.yaml", "replica_count": 2},
        nodes=[{"address": "10.0.0.11", "role": "server"}],
    )
    assert result["valid"] is True
    assert result["type"] == "longhorn"
    assert result["storage_class_name"] == "longhorn"
    assert "Storage(" in result["script"]
    assert "LonghornOptions(" in result["script"]  # nested model, not a raw dict
    assert "backend.create(storage, nodes=nodes)" in result["script"]
    compile(result["script"], "<rendered>", "exec")  # syntax check


def test_build_rejects_unsupported_type():
    with pytest.raises(ValidationError, match="longhorn"):
        build_storage_plan(
            storage={"type": "ceph", "kubeconfig_path": "/tmp/kc.yaml"},
            nodes=[{"address": "10.0.0.11", "role": "server"}],
        )


def test_build_rejects_missing_kubeconfig_path():
    with pytest.raises(ValidationError, match="kubeconfig_path"):
        build_storage_plan(
            storage={"type": "longhorn", "kubeconfig_path": ""},
            nodes=[{"address": "10.0.0.11", "role": "server"}],
        )


def test_build_script_includes_every_storage_field():
    # Same regression class as RKE2's equivalent test -- a hand-typed
    # template can silently drop a field that isn't explicitly named.
    result = build_storage_plan(
        storage={
            "type": "longhorn",
            "kubeconfig_path": "/tmp/kc.yaml",
            "namespace": "custom-longhorn-ns",
            "chart_version": "1.7.2",
        },
        nodes=[{"address": "10.0.0.11", "role": "server"}],
    )
    assert result["valid"] is True
    assert "custom-longhorn-ns" in result["script"]
    assert "1.7.2" in result["script"]
