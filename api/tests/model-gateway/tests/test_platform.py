"""Platform conventions: /health and /metrics, no auth."""


def test_health(make_client):
    client, _ = make_client()
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_metrics(make_client):
    client, _ = make_client()
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
