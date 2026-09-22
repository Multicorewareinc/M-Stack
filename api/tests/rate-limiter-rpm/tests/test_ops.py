"""Ops endpoints: /health, /ready, /metrics (mirrors the model-gateway ops surface)."""

import re

from conftest import FakeConsumer


def test_health(client_factory):
    # AC-7
    client, _ = client_factory()
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_ready_degraded_when_configured_but_inactive(client_factory):
    # Consumer present but not counting -> 503, so the fault can't hide behind a green /health.
    client, _ = client_factory(consumer=FakeConsumer(active=False))
    r = client.get("/ready")
    assert r.status_code == 503
    assert r.json() == {"status": "degraded", "counting": False}


def test_ready_ok_when_active(client_factory):
    client, _ = client_factory(consumer=FakeConsumer(active=True))
    r = client.get("/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "counting": True}


def test_ready_ok_and_inert_when_no_backbone(client_factory):
    # No consumer configured (EVENT_BACKBONE_URL empty) is the deliberate inert path: Ready.
    client, _ = client_factory()  # consumer is None
    r = client.get("/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "counting": False}
    assert client.get("/health").json() == {"status": "ok"}


def test_metrics_exposes_consumer_active_gauge(client_factory):
    client, _ = client_factory()
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "rl_rpm_consumer_active" in r.text


def test_metrics(client_factory):
    # AC-8: issue one /check first so the labelled decision series exists, then scrape.
    client, _ = client_factory()
    check = client.post(
        "/check", json={"principal": "u", "model": "m", "path": "/v1/chat/completions"}
    )
    assert check.status_code == 200 and check.json()["decision"] == "allow"
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers.get("Content-Type", "")
    # Council review fix: assert the actual VALUE of the labelled series the /check call produced,
    # not just that the metric NAME appears somewhere in the scrape (which would still pass even
    # if the counter were never incremented, e.g. only its HELP/TYPE header lines were emitted).
    m = re.search(r'^rl_rpm_decisions_total\{decision="allow"\} (\S+)$', r.text, re.MULTILINE)
    assert m is not None, r.text
    assert float(m.group(1)) >= 1.0
