"""Tests for the Policy MCP tool (agentic/backend/src/mcp_tools/policy.py).
Calls the tool function directly as plain Python -- no MCP transport
involved, no real cluster touched (PolicyBackend is never called)."""

import pytest
from pydantic import ValidationError

from mcp_tools.policy import build_policy_plan


def test_build_rejects_no_backbone_without_the_explicit_opt_out():
    with pytest.raises(ValidationError, match="allow_no_backbone"):
        build_policy_plan(policy={
            "kubeconfig_path": "/tmp/kc.yaml",
            "cache_url": "redis://valkey.platform.svc:6379/0",
            "limits": {"user_default": 60},
        })


def test_build_valid_plan_with_explicit_no_backbone_opt_out():
    result = build_policy_plan(policy={
        "kubeconfig_path": "/tmp/kc.yaml",
        "cache_url": "redis://valkey.platform.svc:6379/0",
        "allow_no_backbone": True,
        "limits": {"user_default": 60},
    })
    assert result["valid"] is True
    assert "RateLimits(" in result["script"]
    assert "user_default=60" in result["script"]
    assert "backend.create(policy)" in result["script"]
    compile(result["script"], "<rendered>", "exec")  # syntax check


def test_build_valid_plan_with_event_backbone_and_rpm_options():
    result = build_policy_plan(policy={
        "kubeconfig_path": "/tmp/kc.yaml",
        "cache_url": "redis://valkey.platform.svc:6379/0",
        "event_backbone_url": "nats://nats.platform.svc:4222",
        "limits": {"user_default": 60, "model_default": 600},
        "options": {"dedupe_ttl": 180},
    })
    assert result["valid"] is True
    assert "event_backbone_url" in result["script"]
    assert "RPMOptions(" in result["script"]
    assert "dedupe_ttl=180" in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_build_rejects_missing_cache_url():
    with pytest.raises(ValidationError, match="cache_url"):
        build_policy_plan(policy={
            "kubeconfig_path": "/tmp/kc.yaml",
            "allow_no_backbone": True,
            "limits": {"user_default": 60},
        })


def test_build_valid_tpm_plan_imports_tpmoptions_not_rpmoptions():
    # Regression test: the script used to hardcode "from multistack.policy
    # import RPMOptions" no matter which type was used -- a tpm policy
    # with real options rendered "options=TPMOptions(...)" while only
    # importing RPMOptions, valid syntax but a NameError if actually run.
    result = build_policy_plan(policy={
        "type": "tpm",
        "kubeconfig_path": "/tmp/kc.yaml",
        "cache_url": "redis://valkey.platform.svc:6379/0",
        "allow_no_backbone": True,
        "limits": {"user_default": 60},
        "options": {"durable_name": "tpm-counter-enriched"},
    })
    assert result["valid"] is True
    assert "from multistack.policy import TPMOptions" in result["script"]
    assert "RPMOptions" not in result["script"]
    assert "TPMOptions(" in result["script"]
    assert "durable_name='tpm-counter-enriched'" in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_build_rejects_tpm_durable_name_colliding_with_rpm():
    # Both consumers read the same stream/subject; sharing a durable name
    # splits it between them and both silently under-count.
    with pytest.raises(ValidationError, match="rpm-counter"):
        build_policy_plan(policy={
            "type": "tpm",
            "kubeconfig_path": "/tmp/kc.yaml",
            "cache_url": "redis://valkey.platform.svc:6379/0",
            "allow_no_backbone": True,
            "options": {"durable_name": "rpm-counter"},
        })


def test_cache_auth_url_renders_its_real_value_not_the_mask():
    """Regression: cache_auth_url is a SecretStr, and model_dump() masks
    it. Rendering the dump wrote SecretStr('**********') into the script
    as a literal -- the limiter would then authenticate with asterisks,
    get NOAUTH on every request, and a rate limiter that cannot read its
    counters fails OPEN: deployed, Ready, enforcing nothing."""
    result = build_policy_plan(policy={
        "kubeconfig_path": "/tmp/kc.yaml",
        "cache_url": "redis://valkey.valkey.svc:6379/0",
        "cache_auth_url": "redis://:s3cret@valkey.valkey.svc:6379/0",
        "allow_no_backbone": True,
    })
    assert result["valid"] is True
    assert "cache_auth_url='redis://:s3cret@valkey.valkey.svc:6379/0'" in result["script"]
    assert "**********" not in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_cache_auth_url_is_omitted_when_not_given():
    result = build_policy_plan(policy={
        "kubeconfig_path": "/tmp/kc.yaml",
        "cache_url": "redis://valkey.valkey.svc:6379/0",
        "allow_no_backbone": True,
    })
    assert result["valid"] is True
    assert "cache_auth_url=None" in result["script"]


ADMIN_CP_POLICY = {
    "kubeconfig_path": "/tmp/kc.yaml",
    "cache_url": "redis://valkey.platform.svc:6379/0",
    "allow_no_backbone": True,
    "admin_cp_url": "http://admin-control-plane.platform.svc.cluster.local:8000",
}


def test_admin_cp_url_without_its_key_is_rejected():
    """The plan lookup would 401 and the limiter would fall back to the
    static limits with no error -- worse than refusing up front."""
    result = build_policy_plan(policy=ADMIN_CP_POLICY)
    assert result["valid"] is False
    assert "admin_cp_service_api_key" in result["error"]


def test_admin_cp_url_with_its_key_renders_both():
    result = build_policy_plan(policy={**ADMIN_CP_POLICY, "admin_cp_service_api_key": "admin-key"})
    assert result["valid"] is True, result
    assert f"admin_cp_url={ADMIN_CP_POLICY['admin_cp_url']!r}," in result["script"]
    assert "admin_cp_service_api_key='admin-key'," in result["script"]
