"""Ops + seed: /health, /metrics (both unauthenticated), and the idempotent startup seed."""

from __future__ import annotations

import re

from conftest import auth_headers, make_client, make_engine

SEED = {
    "Free": (10000, 60, 1000000),
    "Pro": (100000, 600, 50000000),
    "Enterprise": (0, 0, 0),
}


def test_health(client):
    r = client.get("/health")  # no auth
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_metrics(client):
    r = client.get("/metrics")  # no auth
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    # Council review fix: the old `X in text or "# HELP" in text` assertion passes for almost any
    # non-empty response (nearly every prometheus_client scrape has a "# HELP" line, even a
    # near-empty or malformed one) — it would not fail if the app wired a broken/empty registry.
    # Require an actual numeric sample from the GC collector (cross-platform, unlike the
    # Linux-only ProcessCollector metrics).
    m = re.search(r'^python_gc_objects_collected_total\{generation="0"\} (\S+)$', r.text, re.MULTILINE)
    assert m is not None, r.text
    assert float(m.group(1)) >= 0


def test_seed_creates_three_default_plans(client):
    plans = {p["name"]: p for p in client.get("/v1/plans", headers=auth_headers()).json()}
    assert set(plans) == set(SEED)
    for name, (tpm, rpm, quota) in SEED.items():
        assert plans[name]["is_default"] is True
        assert plans[name]["tpm"] == tpm
        assert plans[name]["rpm"] == rpm
        assert plans[name]["quota_monthly_tokens"] == quota


def test_seed_idempotent_second_boot():
    engine = make_engine()

    # First boot: seed runs, then a super-admin edits Free.tpm.
    c1, _ = make_client(engine=engine)
    with c1:
        free = next(p for p in c1.get("/v1/plans", headers=auth_headers()).json() if p["name"] == "Free")
        assert c1.patch(f"/v1/plans/{free['id']}", json={"tpm": 5000}, headers=auth_headers()).status_code == 200

    # Second boot on the SAME database: seed must NOT duplicate or overwrite.
    c2, _ = make_client(engine=engine)
    with c2:
        plans = {p["name"]: p for p in c2.get("/v1/plans", headers=auth_headers()).json()}
        assert set(plans) == set(SEED)  # still exactly three
        assert plans["Free"]["tpm"] == 5000  # edit survived, not overwritten by seed
