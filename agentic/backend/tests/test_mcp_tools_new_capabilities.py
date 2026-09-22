"""Tests for the four capabilities added after the ADR-030 merge:
Enricher, Billing, Route and Accelerator. Calls each tool function
directly as plain Python -- no MCP transport, no real cluster touched."""

import pytest
from pydantic import ValidationError

from mcp_tools.accelerator import build_accelerator_plan
from mcp_tools.billing import build_billing_plan
from mcp_tools.enricher import build_enricher_plan
from mcp_tools.route import build_route_plan

KC = "/tmp/kc.yaml"
NATS = "nats://nats.platform.svc:4222"


# -- Enricher ---------------------------------------------------------------
def test_enricher_builds_and_publishes_the_derived_stream():
    result = build_enricher_plan(enricher={"kubeconfig_path": KC, "event_backbone_url": NATS})
    assert result["valid"] is True
    assert result["enriched_subject"] == "gateway.events.enriched"
    assert "backend.create(enricher)" in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_enricher_without_a_backbone_is_rejected():
    """It exists to consume one stream and publish another. With no
    backbone it is deployed, healthy, and doing nothing -- and the tpm
    limiter and billing downstream then count nothing at all rather than
    erroring."""
    with pytest.raises(ValidationError, match="allow_no_backbone"):
        build_enricher_plan(enricher={"kubeconfig_path": KC})


def test_enricher_carries_the_tokenizer_url_it_is_given():
    result = build_enricher_plan(enricher={
        "kubeconfig_path": KC, "event_backbone_url": NATS,
        "options": {"tokenizer_url": "http://tokenizer.policy.svc.cluster.local:8000"},
    })
    assert result["valid"] is True
    assert "tokenizer_url='http://tokenizer.policy.svc.cluster.local:8000'" in result["script"]


# -- Billing ----------------------------------------------------------------
def test_billing_builds_with_a_secret_name():
    result = build_billing_plan(billing={
        "kubeconfig_path": KC, "existing_secret": "billing-secrets", "event_backbone_url": NATS,
    })
    assert result["valid"] is True
    assert result["endpoint"] == "http://billing.billing.svc.cluster.local:8000"
    assert "billing-secrets" in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_billing_requires_a_secret_name():
    with pytest.raises(ValidationError, match="existing_secret"):
        build_billing_plan(billing={"kubeconfig_path": KC, "event_backbone_url": NATS})


def test_billing_without_a_backbone_is_rejected():
    with pytest.raises(ValidationError, match="allow_no_backbone"):
        build_billing_plan(billing={"kubeconfig_path": KC, "existing_secret": "s"})


# -- Route ------------------------------------------------------------------
def test_route_builds_an_httproute_by_default():
    result = build_route_plan(route={
        "kubeconfig_path": KC, "name": "gw-route", "namespace": "gateway",
        "service": "gateway-model-gateway", "port": 8080, "path_prefix": "/v1",
    })
    assert result["valid"] is True
    assert result["type"] == "httproute"
    compile(result["script"], "<rendered>", "exec")


def test_route_url_is_a_placeholder_until_metallb_assigns_one():
    """MetalLB only assigns the ingress an address when the script runs,
    so this must not be presented as a real host."""
    result = build_route_plan(route={
        "kubeconfig_path": KC, "name": "r", "namespace": "gateway",
        "service": "svc", "port": 80,
    })
    assert "<ingress-address>" in result["route_url"]


def test_route_rejects_a_relative_path_prefix():
    with pytest.raises(ValidationError, match="must start with"):
        build_route_plan(route={
            "kubeconfig_path": KC, "name": "r", "namespace": "gateway",
            "service": "svc", "port": 80, "path_prefix": "v1",
        })


def test_route_rejects_a_hostname_that_is_really_a_url():
    with pytest.raises(ValidationError, match="looks like a URL"):
        build_route_plan(route={
            "kubeconfig_path": KC, "name": "r", "namespace": "gateway",
            "service": "svc", "port": 80, "hostnames": ["https://api.example.com/v1"],
        })


def test_route_rejects_an_empty_service():
    """Route has no model_validator re-running its own validate() at
    construction -- the same gap Portal has -- so build_route_plan must
    call it explicitly or this renders a route with no backend."""
    result = build_route_plan(route={
        "kubeconfig_path": KC, "name": "r", "namespace": "gateway",
        "service": "   ", "port": 80,
    })
    assert result["valid"] is False
    assert "service is required" in result["error"]


# -- Accelerator ------------------------------------------------------------
def test_accelerator_builds_with_defaults():
    result = build_accelerator_plan(accelerator={"kubeconfig_path": KC})
    assert result["valid"] is True
    assert result["namespace"] == "kube-system"
    assert "backend.create(accelerator)" in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_accelerator_imports_from_the_submodule_not_top_level():
    """Accelerator/AcceleratorBackend are NOT re-exported at the top level
    (same as IngressGateway) -- the rendered script would ImportError at
    run time if it used `from multistack import Accelerator`."""
    result = build_accelerator_plan(accelerator={"kubeconfig_path": KC})
    assert "from multistack.accelerator import Accelerator, AcceleratorBackend" in result["script"]
    assert "from multistack import Accelerator" not in result["script"]


def test_accelerator_carries_a_real_node_selector():
    result = build_accelerator_plan(accelerator={
        "kubeconfig_path": KC, "node_selector": {"kubernetes.io/hostname": "gpu-node"},
    })
    assert result["valid"] is True
    assert "'kubernetes.io/hostname': 'gpu-node'" in result["script"]


def test_accelerator_reports_no_endpoint():
    """It publishes a node property, not an address -- there is nothing to
    report or wire, and inventing one would be wrong."""
    result = build_accelerator_plan(accelerator={"kubeconfig_path": KC})
    assert "endpoint" not in result
