"""Health, readiness and metrics."""

from __future__ import annotations

import re

from conftest import AUTH


def test_health(client_factory):
    resp = client_factory().get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_does_not_depend_on_the_index(client_factory):
    assert client_factory(index=False).get("/health").status_code == 200


def test_ready_when_indexed(client_factory):
    resp = client_factory().get("/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


def test_not_ready_until_something_is_indexed(client_factory):
    assert client_factory(index=False).get("/ready").status_code == 503


def test_metrics_count_searches_by_outcome(client_factory):
    client = client_factory()
    assert (
        client.post("/v1/search", json={"query": "gateway replicas"}, headers=AUTH).status_code
        == 200
    )
    text = client.get("/metrics").text
    m = re.search(r'^rag_searches_total\{outcome="results"\} (\S+)$', text, re.MULTILINE)
    assert m is not None, text
    assert float(m.group(1)) >= 1.0
    assert "rag_search_seconds_count" in text


def test_metrics_count_how_results_were_ordered(client_factory):
    client = client_factory()
    client.post("/v1/search", json={"query": "gateway replicas"}, headers=AUTH)
    m = re.search(
        r'^rag_rerank_total\{outcome="off"\} (\S+)$', client.get("/metrics").text, re.MULTILINE
    )
    assert m is not None and float(m.group(1)) >= 1.0
