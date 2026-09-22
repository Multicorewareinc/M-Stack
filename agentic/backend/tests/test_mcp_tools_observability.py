"""Tests for the Observability MCP tool
(agentic/backend/src/mcp_tools/observability.py). Calls the tool function
directly as plain Python -- no MCP transport involved, no real cluster
touched (ObservabilityBackend is never called)."""

import pytest
from pydantic import ValidationError

from mcp_tools.observability import build_observability_plan


def test_build_valid_plan_with_defaults():
    result = build_observability_plan(observability={
        "kubeconfig_path": "/tmp/kc.yaml",
        "storage_class": "longhorn",
    })
    assert result["valid"] is True
    assert result["endpoint"] == "http://kube-prometheus-stack-grafana.monitoring.svc.cluster.local"
    assert "Observability(" in result["script"]
    assert "backend.create(observability)" in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_build_rejects_missing_kubeconfig_path():
    with pytest.raises(ValidationError):
        build_observability_plan(observability={"storage_class": "longhorn"})


def test_build_rejects_bare_number_retention():
    # Regression-style check: Prometheus durations need a unit -- "15"
    # is not 15 days, and the SDK's own validator rejects it outright
    # rather than silently deploying with a default retention.
    with pytest.raises(ValidationError, match="metrics_retention"):
        build_observability_plan(observability={
            "kubeconfig_path": "/tmp/kc.yaml",
            "metrics_retention": "15",
        })


def test_build_rejects_bare_number_volume_size():
    with pytest.raises(ValidationError, match="prometheus_volume_size"):
        build_observability_plan(observability={
            "kubeconfig_path": "/tmp/kc.yaml",
            "prometheus_volume_size": "20",
        })


def test_build_never_invents_a_grafana_password():
    # grafana_admin_password must stay unset here -- the driver
    # generates/reads it back itself; this tool must never fabricate one.
    result = build_observability_plan(observability={"kubeconfig_path": "/tmp/kc.yaml"})
    assert result["valid"] is True
    assert "grafana_admin_password=None" in result["script"]


def test_build_script_includes_options_when_set():
    result = build_observability_plan(observability={
        "kubeconfig_path": "/tmp/kc.yaml",
        "options": {"grafana_service_type": "LoadBalancer"},
    })
    assert result["valid"] is True
    assert "KubePrometheusStackOptions(" in result["script"]
    assert "grafana_service_type='LoadBalancer'" in result["script"]
    compile(result["script"], "<rendered>", "exec")
