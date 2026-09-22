"""Covers specs/billing-plan-subscription-linkage/spec.md: Stripe Subscription create/update/
no-op, the two idempotency-key shapes (D4), the internal endpoint's auth + two inert gates
(D5/D6/D7), and reuse of customer resolution (D2). Layer 1 exercises
`create_or_update_stripe_subscription`'s own classification directly; layer 2 (TestClient)
exercises the full `/internal/v1/subscriptions` endpoint orchestration."""

from __future__ import annotations

import asyncio
import types

import stripe
from sqlalchemy import select

from conftest import (
    FakeStripeEndpointClient,
    FakeStripeSubscriptionClient,
    Layer1FakeMethod,
    make_engine,
    make_stripe_customer,
    make_subscriptions_app,
    service_headers,
)
from db import Base, build_sessionmaker
from models import StripeCustomer
from subscriptions import create_or_update_stripe_subscription

H = service_headers()


async def _fresh_session_with_customer(principal="org-1", stripe_customer_id="cus-1"):
    eng = make_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = build_sessionmaker(eng)
    async with sessionmaker() as s:
        s.add(make_stripe_customer(principal=principal, stripe_customer_id=stripe_customer_id))
        await s.commit()
    return sessionmaker


async def _get_customer(sessionmaker, principal: str) -> StripeCustomer:
    async with sessionmaker() as s:
        return (
            await s.scalars(select(StripeCustomer).where(StripeCustomer.principal == principal))
        ).one()


# --- Layer 1: create_or_update_stripe_subscription (8.2) ---------------------------------------
async def test_first_call_creates_subscription():
    sessionmaker = await _fresh_session_with_customer()
    method = Layer1FakeMethod(returns=types.SimpleNamespace(id="sub_new"))
    client = FakeStripeSubscriptionClient(method)

    async with sessionmaker() as session:
        outcome, sub_id = await create_or_update_stripe_subscription(
            client, session, principal="org-1", stripe_price_id="price_a", stripe_customer_id="cus-1"
        )
        assert outcome == "created" and sub_id == "sub_new"
        await session.commit()

    row = await _get_customer(sessionmaker, "org-1")
    assert row.stripe_subscription_id == "sub_new" and row.stripe_price_id == "price_a"


async def test_same_price_is_noop_with_zero_stripe_calls():
    sessionmaker = await _fresh_session_with_customer()
    create_method = Layer1FakeMethod(returns=types.SimpleNamespace(id="sub_new"))
    client = FakeStripeSubscriptionClient(create_method)

    async with sessionmaker() as session:
        await create_or_update_stripe_subscription(
            client, session, principal="org-1", stripe_price_id="price_a", stripe_customer_id="cus-1"
        )
        await session.commit()

    async with sessionmaker() as session:
        outcome, sub_id = await create_or_update_stripe_subscription(
            client, session, principal="org-1", stripe_price_id="price_a", stripe_customer_id="cus-1"
        )
        assert outcome == "noop" and sub_id == "sub_new"
    assert len(create_method.calls) == 1  # only the first (create) call was made


async def test_different_price_updates_in_place():
    sessionmaker = await _fresh_session_with_customer()
    create_method = Layer1FakeMethod(returns=types.SimpleNamespace(id="sub_new"))
    modify_method = Layer1FakeMethod()
    client = FakeStripeSubscriptionClient(create_method, modify_method)

    async with sessionmaker() as session:
        await create_or_update_stripe_subscription(
            client, session, principal="org-1", stripe_price_id="price_a", stripe_customer_id="cus-1"
        )
        await session.commit()

    async with sessionmaker() as session:
        outcome, sub_id = await create_or_update_stripe_subscription(
            client, session, principal="org-1", stripe_price_id="price_b", stripe_customer_id="cus-1"
        )
        assert outcome == "updated" and sub_id == "sub_new"  # same subscription, not a new one
        await session.commit()
    assert len(modify_method.calls) == 1

    row = await _get_customer(sessionmaker, "org-1")
    assert row.stripe_price_id == "price_b"


async def test_create_classifies_exceptions_correctly():
    cases = [
        (stripe.APIConnectionError("net"), "transient_failure"),
        (stripe.RateLimitError("429"), "transient_failure"),
        (stripe.APIError("5xx"), "transient_failure"),
        (stripe.InvalidRequestError("bad", param=None), "permanent_failure"),
        (stripe.AuthenticationError("bad key"), "permanent_failure"),
    ]
    for i, (exc, expected) in enumerate(cases):
        sessionmaker = await _fresh_session_with_customer(principal=f"org-{i}", stripe_customer_id=f"cus-{i}")
        client = FakeStripeSubscriptionClient(Layer1FakeMethod(raises=exc))
        async with sessionmaker() as session:
            outcome, sub_id = await create_or_update_stripe_subscription(
                client, session, principal=f"org-{i}", stripe_price_id="price_a",
                stripe_customer_id=f"cus-{i}",
            )
            assert outcome == expected and sub_id is None


# --- Idempotency keys (8.3) ---------------------------------------------------------------------
async def test_create_and_update_use_different_idempotency_keys():
    sessionmaker = await _fresh_session_with_customer()
    create_method = Layer1FakeMethod(returns=types.SimpleNamespace(id="sub_new"))
    modify_method = Layer1FakeMethod()
    client = FakeStripeSubscriptionClient(create_method, modify_method)

    async with sessionmaker() as session:
        await create_or_update_stripe_subscription(
            client, session, principal="org-1", stripe_price_id="price_a", stripe_customer_id="cus-1"
        )
        await session.commit()
    async with sessionmaker() as session:
        await create_or_update_stripe_subscription(
            client, session, principal="org-1", stripe_price_id="price_b", stripe_customer_id="cus-1"
        )

    assert create_method.calls[0]["options"] == {"idempotency_key": "billing:subscription:create:org-1:1"}
    assert modify_method.calls[0]["options"] == {
        "idempotency_key": "billing:subscription:update:org-1:price_b"
    }


async def test_two_updates_to_same_target_price_share_one_key():
    sessionmaker = await _fresh_session_with_customer()
    create_method = Layer1FakeMethod(returns=types.SimpleNamespace(id="sub_new"))
    modify_method = Layer1FakeMethod(raises=stripe.APIConnectionError("net"))
    client = FakeStripeSubscriptionClient(create_method, modify_method)

    async with sessionmaker() as session:
        await create_or_update_stripe_subscription(
            client, session, principal="org-1", stripe_price_id="price_a", stripe_customer_id="cus-1"
        )
        await session.commit()

    # Two attempts to move to the SAME target price ("price_b") — e.g. a retry after a
    # transient failure — must carry the identical idempotency key.
    for _ in range(2):
        async with sessionmaker() as session:
            await create_or_update_stripe_subscription(
                client, session, principal="org-1", stripe_price_id="price_b", stripe_customer_id="cus-1"
            )

    assert len(modify_method.calls) == 2
    assert modify_method.calls[0]["options"] == modify_method.calls[1]["options"]


async def test_retry_after_failed_create_uses_a_fresh_idempotency_key():
    # Each create attempt increments a counter folded into the idempotency key, so a retry
    # after a failed attempt (e.g. Stripe rejecting a Customer with no payment method) carries
    # its own key rather than the retry replaying the earlier attempt's result.
    sessionmaker = await _fresh_session_with_customer()
    method = Layer1FakeMethod(raises=stripe.InvalidRequestError("no payment method", param=None))
    client = FakeStripeSubscriptionClient(method)

    async with sessionmaker() as session:
        outcome, _ = await create_or_update_stripe_subscription(
            client, session, principal="org-1", stripe_price_id="price_a", stripe_customer_id="cus-1"
        )
        assert outcome == "permanent_failure"
        await session.commit()

    async with sessionmaker() as session:
        outcome, _ = await create_or_update_stripe_subscription(
            client, session, principal="org-1", stripe_price_id="price_a", stripe_customer_id="cus-1"
        )
        assert outcome == "permanent_failure"

    assert len(method.calls) == 2
    key1 = method.calls[0]["options"]["idempotency_key"]
    key2 = method.calls[1]["options"]["idempotency_key"]
    assert key1 != key2
    assert key1 == "billing:subscription:create:org-1:1"
    assert key2 == "billing:subscription:create:org-1:2"


async def test_create_uses_default_incomplete_payment_behavior():
    # This product has no checkout UI to ever attach a payment method to a Customer up front,
    # so the plain create default (which requires one) would permanently fail for every
    # brand-new org. payment_behavior=default_incomplete lets the create succeed anyway,
    # leaving the Subscription `incomplete` until some future checkout step completes it.
    sessionmaker = await _fresh_session_with_customer()
    method = Layer1FakeMethod(returns=types.SimpleNamespace(id="sub_new"))
    client = FakeStripeSubscriptionClient(method)

    async with sessionmaker() as session:
        await create_or_update_stripe_subscription(
            client, session, principal="org-1", stripe_price_id="price_a", stripe_customer_id="cus-1",
        )

    params = method.calls[0]["args"][0]
    assert params["payment_behavior"] == "default_incomplete"


# --- Layer 2: the full internal endpoint (8.4-8.10) ---------------------------------------------
def test_missing_bearer_is_401():
    client, _ = make_subscriptions_app(stripe_client=FakeStripeEndpointClient())
    with client:
        r = client.post("/internal/v1/subscriptions", json={"org_id": "org-1", "stripe_price_id": "price_a"})
        assert r.status_code == 401


def test_invalid_bearer_is_401():
    client, _ = make_subscriptions_app(stripe_client=FakeStripeEndpointClient())
    with client:
        r = client.post(
            "/internal/v1/subscriptions",
            json={"org_id": "org-1", "stripe_price_id": "price_a"},
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert r.status_code == 401


def test_null_price_is_inert():
    fake = FakeStripeEndpointClient()
    client, engine = make_subscriptions_app(stripe_client=fake)
    with client:
        r = client.post(
            "/internal/v1/subscriptions", json={"org_id": "org-1", "stripe_price_id": None}, headers=H
        )
        assert r.status_code == 200
        assert r.json() == {"outcome": "inert", "stripe_subscription_id": None}
    assert len(fake.customer_method.calls) == 0
    assert len(fake.subscription_create_method.calls) == 0


def test_brand_new_org_gets_customer_and_subscription_created():
    fake = FakeStripeEndpointClient()
    client, engine = make_subscriptions_app(stripe_client=fake)
    with client:
        r = client.post(
            "/internal/v1/subscriptions", json={"org_id": "org-new", "stripe_price_id": "price_a"},
            headers=H,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["outcome"] == "created" and body["stripe_subscription_id"] == "sub_endpoint"

    async def _check():
        sessionmaker = build_sessionmaker(engine)
        return await _get_customer(sessionmaker, "org-new")

    row = asyncio.run(_check())
    assert row.stripe_customer_id == "cus_endpoint"  # the Customer WAS created via reuse


def test_immediate_second_call_same_price_is_noop():
    fake = FakeStripeEndpointClient()
    client, engine = make_subscriptions_app(stripe_client=fake)
    with client:
        client.post(
            "/internal/v1/subscriptions", json={"org_id": "org-new2", "stripe_price_id": "price_a"},
            headers=H,
        )
        r = client.post(
            "/internal/v1/subscriptions", json={"org_id": "org-new2", "stripe_price_id": "price_a"},
            headers=H,
        )
        assert r.status_code == 200 and r.json()["outcome"] == "noop"
    assert len(fake.customer_method.calls) == 1  # reused, not created again
    assert len(fake.subscription_create_method.calls) == 1  # not called a second time


def test_customer_resolution_failure_short_circuits_before_subscription_call():
    fake = FakeStripeEndpointClient(customer_method=Layer1FakeMethod(raises=stripe.APIConnectionError("net")))
    client, _ = make_subscriptions_app(stripe_client=fake)
    with client:
        r = client.post(
            "/internal/v1/subscriptions", json={"org_id": "org-fail", "stripe_price_id": "price_a"},
            headers=H,
        )
        assert r.status_code == 200
        assert r.json() == {"outcome": "transient_failure", "stripe_subscription_id": None}
    assert len(fake.subscription_create_method.calls) == 0


def test_stripe_unset_entirely_is_inert_no_session_touched():
    client, _ = make_subscriptions_app(stripe_client=None)  # no override -> stays None (inert)
    with client:
        r = client.post(
            "/internal/v1/subscriptions", json={"org_id": "org-x", "stripe_price_id": "price_a"},
            headers=H,
        )
        assert r.status_code == 200
        assert r.json() == {"outcome": "inert", "stripe_subscription_id": None}
