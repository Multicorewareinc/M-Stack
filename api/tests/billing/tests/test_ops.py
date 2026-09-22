"""Covers specs/billing-metering/spec.md "Health and metrics are exposed"."""

from __future__ import annotations

import re

from conftest import FakeMsg, StubConsumer, response_event


def test_health(client_factory):
    client = client_factory()
    with client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


def _series_value(text: str, name: str, **labels) -> float | None:
    for line in text.splitlines():
        if not line.startswith(name + "{"):
            continue
        if all(f'{k}="{v}"' in line for k, v in labels.items()):
            return float(line.rsplit(" ", 1)[1])
    return None


async def test_metrics_exposes_labelled_series_values():
    from conftest import make_engine, make_consumer

    consumer, engine, _sm = await make_consumer()
    await consumer._on_msg(FakeMsg(response_event(request_id="m1", usage={"total_tokens": 10, "source": "provider"})))
    await consumer._on_msg(
        FakeMsg(response_event(request_id="m2", usage={"total_tokens": None, "source": "none", "reason": "no_body"}))
    )

    from conftest import make_client

    client = make_client(engine=engine, consumer=StubConsumer(active=True))
    with client:
        text = client.get("/metrics").text
        assert (_series_value(text, "billing_usage_persisted_total", countable="true") or 0) >= 1
        assert (_series_value(text, "billing_usage_persisted_total", countable="false") or 0) >= 1
        assert re.search(r"^billing_consumer_active\s+1\.0$", text, re.MULTILINE)


def test_metrics_consumer_active_zero_when_no_consumer(client_factory):
    client = client_factory()
    with client:
        text = client.get("/metrics").text
        assert re.search(r"^billing_consumer_active\s+0\.0$", text, re.MULTILINE)
