"""Billing metrics: the outbox backlog gauges and Stripe webhook outcomes."""

from __future__ import annotations

from conftest import make_outbox_row
from prometheus_client import REGISTRY
from test_reporter import _seed
from test_webhooks import _ConstructEventStub, _event, _fresh_session
from webhooks import process_webhook


def _value(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


async def test_backlog_gauges_track_pending_rows(reporter_factory):
    # A transient failure leaves the row pending, so the backlog is still 1 after the poll.
    reporter, _eng, sessionmaker, _stub = await reporter_factory(results=["transient_failure"])
    await _seed(sessionmaker, make_outbox_row(request_id="req-backlog"))

    await reporter.run_once()

    assert _value("billing_outbox_pending") == 1
    assert _value("billing_outbox_oldest_pending_seconds") >= 0


async def test_backlog_is_zero_when_drained(reporter_factory):
    reporter, _eng, _sessionmaker, _stub = await reporter_factory(results=["sent"])
    await reporter.run_once()
    assert _value("billing_outbox_pending") == 0
    assert _value("billing_outbox_oldest_pending_seconds") == 0


async def test_invalid_signature_is_counted(monkeypatch):
    import stripe

    stub = _ConstructEventStub(raises=stripe.SignatureVerificationError("bad sig", "sig_header"))
    monkeypatch.setattr(stripe.Webhook, "construct_event", stub)
    labels = {"event_type": "unknown", "outcome": "invalid_signature"}
    before = _value("billing_stripe_webhooks_total", **labels)

    sessionmaker = await _fresh_session()
    async with sessionmaker() as session:
        await process_webhook(b"x", "bad", session, webhook_secret="whsec_test")

    assert _value("billing_stripe_webhooks_total", **labels) == before + 1


async def test_unhandled_event_types_fold_into_other(monkeypatch):
    import stripe

    stub = _ConstructEventStub(event=_event("charge.refunded", subscription_id="sub_x"))
    monkeypatch.setattr(stripe.Webhook, "construct_event", stub)
    labels = {"event_type": "other", "outcome": "ignored"}
    before = _value("billing_stripe_webhooks_total", **labels)

    sessionmaker = await _fresh_session()
    async with sessionmaker() as session:
        status, body = await process_webhook(b"x", "sig", session, webhook_secret="whsec_test")

    assert (status, body) == (200, {"outcome": "ignored"})
    assert _value("billing_stripe_webhooks_total", **labels) == before + 1
