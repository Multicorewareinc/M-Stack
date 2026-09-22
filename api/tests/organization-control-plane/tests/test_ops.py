"""Ops endpoints — unauthenticated /health and /metrics; also exercises the injected-engine boot."""

from __future__ import annotations

import re


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_metrics(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    # Council review fix: require an actual numeric sample, not just a non-empty-looking response
    # — cross-platform via the GC collector (unlike Linux-only ProcessCollector metrics).
    m = re.search(r'^python_gc_objects_collected_total\{generation="0"\} (\S+)$', r.text, re.MULTILINE)
    assert m is not None, r.text
    assert float(m.group(1)) >= 0
