"""Covers specs/billing-stripe-customer-mapping/spec.md and the
specs/billing-stripe-reporting/spec.md delta (add-billing-stripe-customer-mapping): lazy
Stripe Customer creation/reuse, idempotency key stability, customer-creation failure blocking
that row's usage report, the inert gate, and observability. Two independent test layers
(design D7, extended to customer resolution): layer 1 exercises
`create_or_get_stripe_customer`'s own Stripe-exception classification in isolation; layer 2
exercises `StripeReporter._process_row`'s wiring via an injected `customer_resolver` stub that
bypasses classification entirely."""

from __future__ import annotations

import types

from sqlalchemy import select

from conftest import (
    FakeStripeCustomerClient,
    Layer1FakeMethod,
    StubCustomerResolver,
    make_engine,
    make_outbox_row,
    make_stripe_customer,
)
from db import Base, build_sessionmaker
from models import Outbox, StripeCustomer
from reporter import create_or_get_stripe_customer


async def _fresh_session():
    eng = make_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = build_sessionmaker(eng)
    return sessionmaker


async def _seed(sessionmaker, row) -> None:
    async with sessionmaker() as s:
        s.add(row)
        await s.commit()


async def _get_outbox(sessionmaker, request_id: str) -> Outbox:
    async with sessionmaker() as s:
        return (await s.scalars(select(Outbox).where(Outbox.request_id == request_id))).one()


async def _all_stripe_customers(sessionmaker) -> list[StripeCustomer]:
    async with sessionmaker() as s:
        return list(await s.scalars(select(StripeCustomer)))


def _metric_value(text: str, name: str, **labels) -> float:
    for line in text.splitlines():
        if not line.startswith(name + "{"):
            continue
        if all(f'{k}="{v}"' in line for k, v in labels.items()):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


# --- Layer 1: create_or_get_stripe_customer's own exception classification (4.2) --------------
async def test_create_or_get_stripe_customer_classifies_exceptions_correctly():
    import stripe

    cases = [
        (None, "created"),
        (stripe.APIConnectionError("net"), "transient_failure"),
        (stripe.RateLimitError("429"), "transient_failure"),  # must precede InvalidRequestError
        (stripe.APIError("5xx"), "transient_failure"),
        (stripe.InvalidRequestError("bad request", param=None), "permanent_failure"),
        (stripe.AuthenticationError("bad key"), "permanent_failure"),
    ]
    for i, (exc, expected) in enumerate(cases):
        sessionmaker = await _fresh_session()
        method = Layer1FakeMethod(raises=exc, returns=types.SimpleNamespace(id=f"cus_new_{i}"))
        client = FakeStripeCustomerClient(method)
        async with sessionmaker() as session:
            outcome, customer_id = await create_or_get_stripe_customer(
                client, session, principal=f"org-{i}"
            )
            assert outcome == expected, f"{exc!r} -> expected {expected}, got {outcome}"
            if expected == "created":
                assert customer_id == f"cus_new_{i}"
                await session.commit()
            else:
                assert customer_id is None

        if expected == "created":
            rows = await _all_stripe_customers(sessionmaker)
            assert len(rows) == 1
            assert rows[0].principal == f"org-{i}"
            assert rows[0].stripe_customer_id == f"cus_new_{i}"
        else:
            assert await _all_stripe_customers(sessionmaker) == []


# --- Layer 1: reuse avoids a Stripe call entirely, and idempotency key is stable (4.4/4.5) ----
async def test_existing_mapping_is_reused_with_zero_stripe_calls():
    sessionmaker = await _fresh_session()
    await _seed(sessionmaker, make_stripe_customer(principal="org-1", stripe_customer_id="cus_existing"))

    method = Layer1FakeMethod()  # would raise-nothing/return-None; call count is what matters
    client = FakeStripeCustomerClient(method)
    async with sessionmaker() as session:
        outcome, customer_id = await create_or_get_stripe_customer(client, session, principal="org-1")

    assert outcome == "reused"
    assert customer_id == "cus_existing"
    assert len(method.calls) == 0  # no underlying Stripe Customer creation call was made


async def test_customer_creation_idempotency_key_derived_solely_from_principal():
    import stripe

    sessionmaker = await _fresh_session()
    method = Layer1FakeMethod(raises=stripe.APIConnectionError("net"))
    client = FakeStripeCustomerClient(method)

    async with sessionmaker() as session:
        await create_or_get_stripe_customer(client, session, principal="org-1")
    async with sessionmaker() as session:
        # A second attempt (e.g. after a reporter restart) for the SAME principal — no
        # existing mapping was committed (both attempts failed), so this also misses the table.
        await create_or_get_stripe_customer(client, session, principal="org-1")

    assert len(method.calls) == 2
    assert (
        method.calls[0]["options"]
        == method.calls[1]["options"]
        == {"idempotency_key": "billing:customer:org-1"}
    )


# --- Layer 2: reporter wiring (4.3, 4.6, 4.7, 4.8, 4.9, 4.10) ----------------------------------
async def test_new_org_gets_customer_created_and_meter_event_carries_resolved_id(reporter_factory):
    reporter, _eng, sessionmaker, stub = await reporter_factory(
        results=["sent"], customer_results=[("created", "cus_brand_new")],
    )
    await _seed(sessionmaker, make_outbox_row(request_id="req-a", payload={
        "request_id": "req-a", "principal": "org-a", "model": "m1", "total_tokens": 150,
        "source": "provider", "ts": 1234.5,
    }))

    await reporter.run_once()

    assert len(stub.calls) == 1
    assert stub.calls[0]["principal"] == "cus_brand_new"  # resolved id, NOT the raw "org-a"
    row = await _get_outbox(sessionmaker, "req-a")
    assert row.status == "sent"


async def test_existing_mapping_reused_end_to_end(reporter_factory):
    reporter, _eng, sessionmaker, stub = await reporter_factory(
        results=["sent"], customer_results=[("reused", "cus_existing")],
    )
    await _seed(sessionmaker, make_stripe_customer(principal="org-b", stripe_customer_id="cus_existing"))
    await _seed(sessionmaker, make_outbox_row(request_id="req-b", payload={
        "request_id": "req-b", "principal": "org-b", "model": "m1", "total_tokens": 150,
        "source": "provider", "ts": 1234.5,
    }))

    await reporter.run_once()

    assert stub.calls[0]["principal"] == "cus_existing"


async def test_transient_customer_failure_blocks_report_and_backs_off(reporter_factory):
    reporter, _eng, sessionmaker, stub = await reporter_factory(
        results=["sent"], customer_results=[("transient_failure", None)],
        reporter_max_attempts=5,
    )
    await _seed(sessionmaker, make_outbox_row(request_id="req-c"))

    await reporter.run_once()

    assert len(stub.calls) == 0  # report_to_stripe was never called for this row
    row = await _get_outbox(sessionmaker, "req-c")
    assert row.status == "pending"
    assert row.attempts == 1
    assert row.next_attempt_at is not None


async def test_permanent_customer_failure_blocks_report_and_fails_immediately(reporter_factory):
    reporter, _eng, sessionmaker, stub = await reporter_factory(
        results=["sent"], customer_results=[("permanent_failure", None)],
    )
    await _seed(sessionmaker, make_outbox_row(request_id="req-d"))

    await reporter.run_once()

    assert len(stub.calls) == 0
    row = await _get_outbox(sessionmaker, "req-d")
    assert row.status == "failed"
    assert row.attempts == 0


async def test_customer_resolution_inert_when_stripe_key_unset():
    """The REAL inert gate (task 4.8): create_app's owns-it path, default Settings() (empty
    stripe_secret_key), no reporter= override — customer resolution must not run at all."""
    from db import build_sessionmaker
    from main import create_app
    from settings import Settings

    eng = make_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = build_sessionmaker(eng)

    app = create_app(Settings(), engine=eng)
    reporter = app.state.reporter
    assert reporter is not None

    await _seed(sessionmaker, make_outbox_row(request_id="req-e"))
    await reporter.run_once()

    assert await _all_stripe_customers(sessionmaker) == []
    row = await _get_outbox(sessionmaker, "req-e")
    assert row.status == "pending"


async def test_customer_resolution_metrics_reflect_outcomes(reporter_factory):
    reporter, _eng, sessionmaker, stub = await reporter_factory(
        results=["sent", "sent", "sent"],
        customer_results=[("created", "cus-1"), ("reused", "cus-2"), ("permanent_failure", None)],
    )
    await _seed(sessionmaker, make_outbox_row(request_id="req-f1"))
    await _seed(sessionmaker, make_outbox_row(request_id="req-f2"))
    await _seed(sessionmaker, make_outbox_row(request_id="req-f3"))

    await reporter.run_once()

    from prometheus_client import generate_latest

    text = generate_latest().decode()
    assert _metric_value(text, "billing_stripe_customer_resolved_total", outcome="created") >= 1
    assert _metric_value(text, "billing_stripe_customer_resolved_total", outcome="reused") >= 1
    assert _metric_value(text, "billing_stripe_customer_resolved_total", outcome="failed") >= 1


async def test_customer_resolution_completes_before_reporting(reporter_factory):
    order: list[str] = []

    reporter, _eng, sessionmaker, stub = await reporter_factory(results=["sent"])
    customer_stub = StubCustomerResolver([("reused", "cus-existing")])

    async def _tracking_customer_resolver(client, session, *, principal):
        order.append("customer_resolved")
        return await customer_stub(client, session, principal=principal)

    async def _tracking_report_fn(*args, **kwargs):
        order.append("reported")
        return await stub(*args, **kwargs)

    reporter._customer_resolver = _tracking_customer_resolver
    reporter._report_fn = _tracking_report_fn
    await _seed(sessionmaker, make_stripe_customer(principal="u1", stripe_customer_id="cus-existing"))
    await _seed(sessionmaker, make_outbox_row(request_id="req-g"))

    await reporter.run_once()

    assert order == ["customer_resolved", "reported"]
