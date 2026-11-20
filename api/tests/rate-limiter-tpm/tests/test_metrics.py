"""Valkey faults the limiter swallows are counted, so a fail-open limiter is visible."""

from prometheus_client import REGISTRY


def _errors(op):
    return REGISTRY.get_sample_value("rl_tpm_valkey_errors_total", {"op": op}) or 0.0


def test_fail_open_check_is_counted(client_factory):
    client, _redis = client_factory(fail=True, rate_limits='{"user_default":1}')
    before = _errors("check")
    resp = client.post("/check", json={"principal": "u1", "model": None, "path": "/v1/x"})
    assert resp.json() == {"decision": "allow"}
    assert _errors("check") == before + 1


async def test_dropped_count_is_counted(service_factory):
    service, _redis = service_factory(fail=True, rate_limits='{"user_default":1000}')
    before = _errors("count")
    await service.count("u1", "m1", "req-1", 100)
    assert _errors("count") == before + 1
