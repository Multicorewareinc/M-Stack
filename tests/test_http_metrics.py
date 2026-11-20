"""The HTTP metrics middleware every first-party service ships (`src/http_metrics.py`).

Each service image carries only its own `src/`, so the module is copied
rather than shared. These check the copies have not drifted -- dashboards
and alerts query the series by name across every service at once -- and
that the middleware labels by route template, not raw path.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVICES = [
    "model-gateway", "tokenizer", "enricher", "rate-limiter-rpm", "rate-limiter-tpm",
    "billing", "organization-control-plane", "admin-control-plane",
]
COPIES = [ROOT / "api/microservices" / s / "src/http_metrics.py" for s in SERVICES]


def test_every_service_ships_the_same_module():
    reference = COPIES[0].read_text()
    drifted = [str(p.relative_to(ROOT)) for p in COPIES[1:] if p.read_text() != reference]
    assert not drifted, f"http_metrics.py differs from {COPIES[0].relative_to(ROOT)}: {drifted}"


def test_every_service_installs_it():
    for service in SERVICES:
        main = (ROOT / "api/microservices" / service / "src/main.py").read_text()
        assert "app.add_middleware(HttpMetricsMiddleware)" in main, service


@pytest.fixture(scope="module")
def http_metrics():
    pytest.importorskip("fastapi")
    pytest.importorskip("prometheus_client")
    pytest.importorskip("httpx")
    spec = importlib.util.spec_from_file_location("http_metrics_under_test", COPIES[0])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _app(http_metrics):
    from fastapi import FastAPI, HTTPException

    app = FastAPI()
    app.add_middleware(http_metrics.HttpMetricsMiddleware)

    @app.get("/items/{item_id}")
    async def item(item_id: int):
        if item_id == 404:
            raise HTTPException(status_code=404)
        return {"id": item_id}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("unhandled")

    @app.get("/metrics")
    async def metrics():
        return {}

    return app


def _count(http_metrics, method, route, status):
    return http_metrics.REQUESTS.labels(method, route, status)._value.get()


def test_labels_by_route_template_and_status(http_metrics):
    from fastapi.testclient import TestClient

    client = TestClient(_app(http_metrics), raise_server_exceptions=False)
    ok_before = _count(http_metrics, "GET", "/items/{item_id}", "200")
    missing_before = _count(http_metrics, "GET", "/items/{item_id}", "404")

    client.get("/items/1")
    client.get("/items/2")
    client.get("/items/404")

    assert _count(http_metrics, "GET", "/items/{item_id}", "200") == ok_before + 2
    assert _count(http_metrics, "GET", "/items/{item_id}", "404") == missing_before + 1


def test_unmatched_paths_share_one_label(http_metrics):
    from fastapi.testclient import TestClient

    client = TestClient(_app(http_metrics))
    before = _count(http_metrics, "GET", http_metrics.UNMATCHED, "404")
    client.get("/no/such/1")
    client.get("/no/such/2")
    assert _count(http_metrics, "GET", http_metrics.UNMATCHED, "404") == before + 2


def test_an_unhandled_exception_is_a_500(http_metrics):
    from fastapi.testclient import TestClient

    client = TestClient(_app(http_metrics), raise_server_exceptions=False)
    before = _count(http_metrics, "GET", "/boom", "500")
    assert client.get("/boom").status_code == 500
    assert _count(http_metrics, "GET", "/boom", "500") == before + 1
    assert http_metrics.IN_FLIGHT._value.get() == 0


def test_scrapes_are_not_counted(http_metrics):
    from fastapi.testclient import TestClient

    client = TestClient(_app(http_metrics))
    before = _count(http_metrics, "GET", "/metrics", "200")
    client.get("/metrics")
    assert _count(http_metrics, "GET", "/metrics", "200") == before
