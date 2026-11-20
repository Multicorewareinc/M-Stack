"""Valkey faults the limiter swallows are counted, so a fail-open limiter is visible."""

import json

from prometheus_client import REGISTRY


def _errors(op):
    return REGISTRY.get_sample_value("rl_rpm_valkey_errors_total", {"op": op}) or 0.0


async def test_fail_open_check_is_counted(service_factory):
    svc, _ = service_factory(fail=True, rate_limits=json.dumps({"user_default": 1}))
    before = _errors("check")
    assert (await svc.check("u1", "m1", "/v1/chat/completions")) == {"decision": "allow"}
    assert _errors("check") == before + 1


async def test_dropped_count_is_counted(service_factory):
    svc, _ = service_factory(fail=True, rate_limits=json.dumps({"user_default": 1}))
    before = _errors("count")
    await svc.count("u1", "m1", "/v1/chat/completions", "r1")
    assert _errors("count") == before + 1


async def test_healthy_valkey_counts_nothing(service_factory):
    svc, _ = service_factory(rate_limits=json.dumps({"user_default": 5}))
    before = _errors("check")
    await svc.check("u1", "m1", "/v1/chat/completions")
    assert _errors("check") == before
