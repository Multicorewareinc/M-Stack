"""Tests for the Tokenizer MCP tool (agentic/backend/src/mcp_tools/tokenizer.py).
Calls the tool function directly as plain Python -- no MCP transport
involved, no real cluster touched (TokenizerBackend is never called)."""

import pytest
from pydantic import ValidationError

from mcp_tools.tokenizer import build_tokenizer_plan


def test_build_valid_plan_with_defaults():
    result = build_tokenizer_plan(tokenizer={"kubeconfig_path": "/tmp/kc.yaml"})
    assert result["valid"] is True
    assert result["endpoint"] == "http://tokenizer.policy.svc.cluster.local:8000"
    assert "Tokenizer(" in result["script"]
    assert "backend.create(tokenizer)" in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_build_rejects_missing_kubeconfig_path():
    with pytest.raises(ValidationError):
        build_tokenizer_plan(tokenizer={})


def test_build_script_includes_options_when_set():
    result = build_tokenizer_plan(tokenizer={
        "kubeconfig_path": "/tmp/kc.yaml",
        "options": {"default_encoding": "o200k_base"},
    })
    assert result["valid"] is True
    assert "TiktokenOptions(" in result["script"]
    assert "default_encoding='o200k_base'" in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_build_rejects_unknown_type():
    with pytest.raises(ValidationError):
        build_tokenizer_plan(tokenizer={"kubeconfig_path": "/tmp/kc.yaml", "type": "sentencepiece"})
