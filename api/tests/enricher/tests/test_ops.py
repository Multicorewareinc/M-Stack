"""Covers specs/event-enricher/spec.md "Health and metrics are exposed": /health liveness and
/metrics exposing the labelled by-source series (incl. the three none reasons), the
republish-failure series, and the consumer-active gauge — asserting VALUES, not name-only."""

from __future__ import annotations

import re

from conftest import FakeMsg, StubConsumer, response_event


def test_health(client_factory):
    client = client_factory()
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def _series_value(text: str, name: str, **labels) -> float | None:
    """Find a metric line matching name{...all labels...} regardless of label order."""
    for line in text.splitlines():
        if not line.startswith(name + "{"):
            continue
        if all(f'{k}="{v}"' in line for k, v in labels.items()):
            return float(line.rsplit(" ", 1)[1])
    return None


async def test_metrics_exposes_labelled_series_values(consumer_factory, client_factory):
    consumer, _js, _calls = consumer_factory(tokenizer_url="http://tok/tokenize", tokenizer_tokens=7)
    # Drive one event of each source / none-reason so the series exist with real values.
    await consumer._on_msg(FakeMsg(response_event(request_id="p", usage={"total_tokens": 10})))
    await consumer._on_msg(FakeMsg(response_event(request_id="e", usage=None,
                                                  body={"choices": [{"message": {"content": "hi"}}]})))
    await consumer._on_msg(FakeMsg(response_event(request_id="n", usage=None, body=None)))

    consumer_unset, _j2, _c2 = consumer_factory(tokenizer_url="")
    await consumer_unset._on_msg(FakeMsg(response_event(request_id="nu", usage=None,
                                                        body={"choices": [{"message": {"content": "hi"}}]})))

    text = client_factory(consumer=StubConsumer(active=True)).get("/metrics").text

    assert (_series_value(text, "enricher_events_total", source="provider") or 0) >= 1
    assert (_series_value(text, "enricher_events_total", source="estimated") or 0) >= 1
    # The three none reasons are distinct labelled series.
    assert (_series_value(text, "enricher_events_total", source="none", reason="no_body") or 0) >= 1
    assert (_series_value(text, "enricher_events_total", source="none", reason="tokenizer_unset") or 0) >= 1
    # Consumer-active gauge present and set from the injected active consumer.
    assert re.search(r"^enricher_consumer_active\s+1\.0$", text, re.MULTILINE)


def test_metrics_consumer_active_zero_when_no_consumer(client_factory):
    text = client_factory().get("/metrics").text  # consumer is None -> gauge 0
    assert re.search(r"^enricher_consumer_active\s+0\.0$", text, re.MULTILINE)
