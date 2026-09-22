"""Covers specs/rate-limiter-tpm/spec.md: health, ready, and metrics endpoints."""

from __future__ import annotations

import re

from conftest import FakeConsumer


def test_health(client_factory):
    client, _redis = client_factory()
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_ready_degraded_when_configured_but_inactive(client_factory):
    # Consumer present but not counting -> 503, so the fault can't hide behind a green /health.
    client, _redis = client_factory(consumer=FakeConsumer(active=False))
    resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json() == {"status": "degraded", "counting": False}


def test_ready_ok_when_active(client_factory):
    client, _redis = client_factory(consumer=FakeConsumer(active=True))
    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "counting": True}


def test_ready_ok_and_inert_when_no_backbone(client_factory):
    # No consumer configured (EVENT_BACKBONE_URL empty) is the deliberate inert path: Ready.
    client, _redis = client_factory()  # consumer is None
    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "counting": False}
    assert client.get("/health").json() == {"status": "ok"}


def test_metrics(client_factory):
    client, _redis = client_factory(rate_limits='{"user_default":1}')
    check = client.post("/check", json={"principal": "u1", "model": None, "path": "/v1/x"})
    assert check.status_code == 200 and check.json()["decision"] == "allow"
    resp = client.get("/metrics")
    assert resp.status_code == 200
    # Council review fix: assert the actual VALUE of the labelled series, not just that the
    # metric name appears somewhere in the scrape (which would pass even if never incremented).
    m = re.search(r'^rl_tpm_decisions_total\{decision="allow"\} (\S+)$', resp.text, re.MULTILINE)
    assert m is not None, resp.text
    assert float(m.group(1)) >= 1.0


def test_metrics_exposes_consumer_active_gauge(client_factory):
    client, _redis = client_factory()
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "rl_tpm_consumer_active" in resp.text
