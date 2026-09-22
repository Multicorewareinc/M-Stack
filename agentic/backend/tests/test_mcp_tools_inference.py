"""Tests for the Inference MCP tool (agentic/backend/src/mcp_tools/inference.py).
Calls the tool function directly as plain Python -- no MCP transport
involved, no real cluster touched (InferenceBackend is never called)."""

import pytest
from pydantic import ValidationError

from mcp_tools.inference import build_inference_plan


def test_build_valid_plan_with_default_model_and_no_nodes():
    result = build_inference_plan(inference={"kubeconfig_path": "/tmp/kc.yaml"})
    assert result["valid"] is True
    assert result["model"] == "Qwen/Qwen2.5-0.5B-Instruct"
    assert result["endpoint"] == "http://vllm.inference.svc.cluster.local:8000"
    assert "Inference(" in result["script"]
    assert "backend.create(inference)" in result["script"]
    assert "RKE2Node" not in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_build_valid_plan_with_nodes_renders_the_prerequisite_check_call():
    result = build_inference_plan(
        inference={"kubeconfig_path": "/tmp/kc.yaml"},
        nodes=[{"address": "10.0.0.11", "role": "server"}],
    )
    assert result["valid"] is True
    assert "RKE2Node(address='10.0.0.11'" in result["script"]
    assert "backend.create(inference, nodes=nodes)" in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_build_valid_plan_with_s3_weights():
    result = build_inference_plan(inference={
        "kubeconfig_path": "/tmp/kc.yaml",
        "model": "s3://models/Qwen2.5-0.5B",
        "s3_endpoint_url": "https://minio.minio.svc.cluster.local",
        "s3_secret_name": "minio-creds",
    })
    assert result["valid"] is True
    assert "s3_endpoint_url='https://minio.minio.svc.cluster.local'" in result["script"]
    assert "s3_secret_name='minio-creds'" in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_build_rejects_gpu_count_on_cpu_device():
    with pytest.raises(ValidationError, match="device is 'cpu'"):
        build_inference_plan(inference={
            "kubeconfig_path": "/tmp/kc.yaml",
            "device": "cpu",
            "gpu_count": 2,
        })


def test_build_rejects_s3_model_without_endpoint():
    with pytest.raises(ValidationError, match="s3_endpoint_url is unset"):
        build_inference_plan(inference={
            "kubeconfig_path": "/tmp/kc.yaml",
            "model": "s3://models/x",
        })


def test_build_rejects_memory_too_small_for_kv_cache_and_shm():
    with pytest.raises(ValidationError, match="memory_gb"):
        build_inference_plan(inference={
            "kubeconfig_path": "/tmp/kc.yaml",
            "memory_gb": 4,
            "kv_cache_gb": 4,
            "shm_gb": 2,
        })


def test_build_script_includes_every_field_the_inference_actually_has():
    result = build_inference_plan(inference={
        "kubeconfig_path": "/tmp/kc.yaml",
        "device": "gpu",
        "gpu_count": 1,
        "model": "Qwen/Qwen2.5-7B-Instruct",
    })
    assert result["valid"] is True
    assert "device='gpu'" in result["script"]
    assert "gpu_count=1" in result["script"]
    assert "VLLMOptions(" in result["script"]
