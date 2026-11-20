"""Enricher metrics: processing time, and build errors that are acked (dropped) rather than retried."""

from __future__ import annotations

from conftest import FakeMsg, response_event
from prometheus_client import REGISTRY


def _value(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


async def test_processing_time_is_observed_per_response_event(consumer_factory):
    consumer, _js, _calls = consumer_factory()
    before = _value("enricher_process_seconds_count")
    await consumer._on_msg(FakeMsg(response_event(usage={"total_tokens": 3})))
    await consumer._on_msg(FakeMsg({"type": "request", "request_id": "r"}))  # skipped: not timed
    assert _value("enricher_process_seconds_count") == before + 1


async def test_a_build_error_is_counted_and_acked(consumer_factory, monkeypatch):
    consumer, js, _calls = consumer_factory()

    async def broken(_event):
        raise ValueError("cannot build")

    monkeypatch.setattr(consumer, "_build_enriched", broken)
    before = _value("enricher_build_errors_total")
    msg = FakeMsg(response_event())
    await consumer._on_msg(msg)  # returns, does not raise
    assert _value("enricher_build_errors_total") == before + 1
    assert msg.acked
    assert js.published == []
