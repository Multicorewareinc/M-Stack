"""Covers specs/billing-stripe-webhooks/spec.md: signature verification, the inert gate,
idempotent processing, subscription-status sync, the untracked-subscription no-op, and that
metering/reporting behavior is unaffected. Signature verification itself is stubbed
(monkeypatching `stripe.Webhook.construct_event`) rather than computing real HMAC signatures —
this codebase's established "inject a minimal stand-in for the external dependency" convention."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from conftest import make_engine, make_stripe_customer
from db import Base, build_sessionmaker
from models import StripeCustomer, WebhookEvent
from webhooks import process_webhook


async def _fresh_session():
    eng = make_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return build_sessionmaker(eng)


class _FakeStripeEvent:
    """Mimics the real stripe.Event's interface as production code actually uses it: subscript
    access (event["id"], etc.) and .to_dict(). Deliberately NOT iterable and has no .keys(), so
    dict(event) fails here exactly like it does against the real SDK object — a plain dict
    stand-in wouldn't catch code that relies on dict(event) working."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def __getitem__(self, key):
        return self._data[key]

    def to_dict(self) -> dict:
        return dict(self._data)


def _event(event_type: str, *, event_id=None, subscription_id=None, invoice_subscription_id=None, status="active"):
    event_id = event_id or f"evt_{uuid.uuid4().hex}"
    if event_type == "invoice.payment_failed":
        obj = {"id": f"in_{uuid.uuid4().hex}", "subscription": invoice_subscription_id}
    else:
        obj = {"id": subscription_id, "status": status}
    # A real Stripe object's to_dict() can carry Decimal fields (e.g. tax percentages), which
    # aren't JSON-serializable as-is — included here so these tests exercise that path too,
    # not just a real delivery.
    obj["tax_percentage"] = Decimal("8.5")
    return _FakeStripeEvent({"id": event_id, "type": event_type, "data": {"object": obj}})


class _ConstructEventStub:
    """Stands in for `stripe.Webhook.construct_event` — returns a fixed event dict, or raises a
    given exception (simulating an invalid/missing signature)."""

    def __init__(self, *, event: dict | None = None, raises: Exception | None = None) -> None:
        self._event = event
        self._raises = raises
        self.calls: list[dict] = []

    def __call__(self, payload, sig_header, secret):
        self.calls.append({"payload": payload, "sig_header": sig_header, "secret": secret})
        if self._raises is not None:
            raise self._raises
        return self._event


async def _count_webhook_events(sessionmaker) -> int:
    async with sessionmaker() as s:
        return len(list(await s.scalars(select(WebhookEvent))))


async def _get_customer(sessionmaker, stripe_subscription_id: str) -> StripeCustomer:
    async with sessionmaker() as s:
        return (
            await s.scalars(
                select(StripeCustomer).where(
                    StripeCustomer.stripe_subscription_id == stripe_subscription_id
                )
            )
        ).one()


async def test_invalid_signature_is_rejected(monkeypatch):
    import stripe

    stub = _ConstructEventStub(raises=stripe.SignatureVerificationError("bad sig", "sig_header"))
    monkeypatch.setattr(stripe.Webhook, "construct_event", stub)

    sessionmaker = await _fresh_session()
    async with sessionmaker() as session:
        status_code, body = await process_webhook(
            b"irrelevant", "bad-sig", session, webhook_secret="whsec_test"
        )
    assert status_code == 400
    assert await _count_webhook_events(sessionmaker) == 0


async def test_router_returns_501_when_webhook_secret_unset():
    from conftest import make_client

    client = make_client()  # default Settings(): stripe_webhook_secret unset
    with client:
        r = client.post("/webhooks/stripe", content=b"not json at all \xff\xfe")
        assert r.status_code == 501


async def test_duplicate_event_is_a_noop(monkeypatch):
    import stripe

    event = _event("customer.subscription.updated", subscription_id="sub_dup", status="active")
    stub = _ConstructEventStub(event=event)
    monkeypatch.setattr(stripe.Webhook, "construct_event", stub)

    sessionmaker = await _fresh_session()
    async with sessionmaker() as session:
        status_code, body = await process_webhook(b"{}", "sig", session, webhook_secret="whsec_test")
    assert status_code == 200 and body["outcome"] == "untracked_subscription"
    assert await _count_webhook_events(sessionmaker) == 1

    async with sessionmaker() as session:
        status_code, body = await process_webhook(b"{}", "sig", session, webhook_secret="whsec_test")
    assert status_code == 200 and body["outcome"] == "duplicate"
    assert await _count_webhook_events(sessionmaker) == 1  # not 2


async def test_subscription_updated_syncs_status(monkeypatch):
    import stripe

    sessionmaker = await _fresh_session()
    async with sessionmaker() as s:
        s.add(make_stripe_customer(principal="org-1", stripe_customer_id="cus-1"))
        row = (await s.scalars(select(StripeCustomer))).one()
        row.stripe_subscription_id = "sub_abc"
        await s.commit()

    event = _event("customer.subscription.updated", subscription_id="sub_abc", status="past_due")
    monkeypatch.setattr(stripe.Webhook, "construct_event", _ConstructEventStub(event=event))

    async with sessionmaker() as session:
        status_code, body = await process_webhook(b"{}", "sig", session, webhook_secret="whsec_test")
    assert status_code == 200 and body["outcome"] == "updated"

    updated = await _get_customer(sessionmaker, "sub_abc")
    assert updated.subscription_status == "past_due"


async def test_subscription_deleted_syncs_status(monkeypatch):
    import stripe

    sessionmaker = await _fresh_session()
    async with sessionmaker() as s:
        s.add(make_stripe_customer(principal="org-2", stripe_customer_id="cus-2"))
        row = (await s.scalars(select(StripeCustomer))).one()
        row.stripe_subscription_id = "sub_del"
        await s.commit()

    event = _event("customer.subscription.deleted", subscription_id="sub_del", status="canceled")
    monkeypatch.setattr(stripe.Webhook, "construct_event", _ConstructEventStub(event=event))

    async with sessionmaker() as session:
        await process_webhook(b"{}", "sig", session, webhook_secret="whsec_test")

    updated = await _get_customer(sessionmaker, "sub_del")
    assert updated.subscription_status == "canceled"


async def test_payment_failed_sets_past_due(monkeypatch):
    import stripe

    sessionmaker = await _fresh_session()
    async with sessionmaker() as s:
        s.add(make_stripe_customer(principal="org-3", stripe_customer_id="cus-3"))
        row = (await s.scalars(select(StripeCustomer))).one()
        row.stripe_subscription_id = "sub_invoice"
        await s.commit()

    event = _event("invoice.payment_failed", invoice_subscription_id="sub_invoice")
    monkeypatch.setattr(stripe.Webhook, "construct_event", _ConstructEventStub(event=event))

    async with sessionmaker() as session:
        status_code, body = await process_webhook(b"{}", "sig", session, webhook_secret="whsec_test")
    assert status_code == 200 and body["outcome"] == "updated"

    updated = await _get_customer(sessionmaker, "sub_invoice")
    assert updated.subscription_status == "past_due"


async def test_untracked_subscription_is_logged_noop(monkeypatch):
    import stripe

    event = _event("customer.subscription.updated", subscription_id="sub_unknown", status="active")
    monkeypatch.setattr(stripe.Webhook, "construct_event", _ConstructEventStub(event=event))

    sessionmaker = await _fresh_session()
    async with sessionmaker() as session:
        status_code, body = await process_webhook(b"{}", "sig", session, webhook_secret="whsec_test")
    assert status_code == 200 and body["outcome"] == "untracked_subscription"
    assert await _count_webhook_events(sessionmaker) == 1  # still recorded


async def test_unhandled_event_type_is_ignored(monkeypatch):
    import stripe

    event = _event("charge.succeeded", subscription_id="sub_irrelevant")
    monkeypatch.setattr(stripe.Webhook, "construct_event", _ConstructEventStub(event=event))

    sessionmaker = await _fresh_session()
    async with sessionmaker() as session:
        status_code, body = await process_webhook(b"{}", "sig", session, webhook_secret="whsec_test")
    assert status_code == 200 and body["outcome"] == "ignored"
    assert await _count_webhook_events(sessionmaker) == 1


async def test_subscription_status_does_not_affect_reporter(reporter_factory):
    from conftest import make_outbox_row

    reporter, _eng, sessionmaker, stub = await reporter_factory(
        results=["sent"], customer_results=[("reused", "cus_canceled_org")],
    )
    async with sessionmaker() as s:
        c = make_stripe_customer(principal="u1", stripe_customer_id="cus_canceled_org")
        c.subscription_status = "canceled"
        s.add(c)
        s.add(make_outbox_row(request_id="req-canceled"))
        await s.commit()

    await reporter.run_once()

    assert len(stub.calls) == 1  # reported exactly as any other row would be
    assert stub.calls[0]["principal"] == "cus_canceled_org"
