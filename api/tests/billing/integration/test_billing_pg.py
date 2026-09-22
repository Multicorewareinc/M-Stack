"""Postgres integration test — the ONE place the asyncpg driver and the Alembic-applied
schema actually execute. NOT part of the offline `testpaths` (that suite runs on aiosqlite);
runs only against a real Postgres.

Preconditions (done by the namespaced harness before this runs):
  1. A Postgres reachable at $BILLING_PG_DSN (asyncpg DSN).
  2. `alembic upgrade head` already applied against that database.

Run: `python -m pytest api/tests/billing/integration -q` with BILLING_PG_DSN set.
Skips (does not fail) when BILLING_PG_DSN is absent, so it never breaks the offline suite.
"""

from __future__ import annotations

import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")

DSN = os.environ.get("BILLING_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="BILLING_PG_DSN not set — Postgres integration only")


async def test_usage_outbox_crud_and_request_id_unique_constraint():
    conn = await asyncpg.connect(DSN)
    try:
        usage_id = uuid.uuid4()
        outbox_id = uuid.uuid4()
        request_id = f"pg-req-{uuid.uuid4()}"

        # Migration applied by Alembic must have created both tables.
        await conn.execute(
            "INSERT INTO usage (id, request_id, principal, model, total_tokens, source, "
            "created_at) VALUES ($1,$2,$3,$4,$5,$6,now())",
            usage_id, request_id, "u1", "m1", 150, "provider",
        )
        await conn.execute(
            "INSERT INTO outbox (id, request_id, payload, status, attempts, created_at) "
            "VALUES ($1,$2,$3,'pending',0,now())",
            outbox_id, request_id, '{"request_id": "%s"}' % request_id,
        )

        got = await conn.fetchval("SELECT source FROM usage WHERE id=$1", usage_id)
        assert got == "provider"

        # DB-enforced UNIQUE(request_id): a second insert with the same request_id must fail.
        with pytest.raises(asyncpg.exceptions.UniqueViolationError):
            await conn.execute(
                "INSERT INTO usage (id, request_id, source, created_at) VALUES ($1,$2,'none',now())",
                uuid.uuid4(), request_id,
            )

        await conn.execute("DELETE FROM outbox WHERE id=$1", outbox_id)
        await conn.execute("DELETE FROM usage WHERE id=$1", usage_id)
    finally:
        await conn.close()


async def test_stripe_customers_unique_principal_constraint():
    conn = await asyncpg.connect(DSN)
    try:
        customer_id = uuid.uuid4()
        principal = f"pg-org-{uuid.uuid4()}"

        # Migration applied by Alembic must have created stripe_customers.
        await conn.execute(
            "INSERT INTO stripe_customers (id, principal, stripe_customer_id, created_at, "
            "updated_at) VALUES ($1,$2,$3,now(),now())",
            customer_id, principal, "cus_pg_test",
        )

        got = await conn.fetchval(
            "SELECT stripe_customer_id FROM stripe_customers WHERE id=$1", customer_id
        )
        assert got == "cus_pg_test"

        # DB-enforced UNIQUE(principal): a second insert with the same principal must fail.
        with pytest.raises(asyncpg.exceptions.UniqueViolationError):
            await conn.execute(
                "INSERT INTO stripe_customers (id, principal, stripe_customer_id, created_at, "
                "updated_at) VALUES ($1,$2,$3,now(),now())",
                uuid.uuid4(), principal, "cus_pg_test_2",
            )

        await conn.execute("DELETE FROM stripe_customers WHERE id=$1", customer_id)
    finally:
        await conn.close()


async def test_stripe_customers_subscription_columns_nullable_then_settable():
    conn = await asyncpg.connect(DSN)
    try:
        customer_id = uuid.uuid4()
        principal = f"pg-org-sub-{uuid.uuid4()}"

        # Migration 0003 must have added these three nullable columns.
        await conn.execute(
            "INSERT INTO stripe_customers (id, principal, stripe_customer_id, created_at, "
            "updated_at) VALUES ($1,$2,$3,now(),now())",
            customer_id, principal, "cus_pg_sub_test",
        )
        row = await conn.fetchrow(
            "SELECT stripe_subscription_id, stripe_price_id, subscription_status "
            "FROM stripe_customers WHERE id=$1", customer_id,
        )
        assert row["stripe_subscription_id"] is None
        assert row["stripe_price_id"] is None
        assert row["subscription_status"] is None

        await conn.execute(
            "UPDATE stripe_customers SET stripe_subscription_id=$1, stripe_price_id=$2, "
            "subscription_status=$3 WHERE id=$4",
            "sub_pg_test", "price_pg_test", "active", customer_id,
        )
        row = await conn.fetchrow(
            "SELECT stripe_subscription_id, stripe_price_id, subscription_status "
            "FROM stripe_customers WHERE id=$1", customer_id,
        )
        assert row["stripe_subscription_id"] == "sub_pg_test"
        assert row["stripe_price_id"] == "price_pg_test"
        assert row["subscription_status"] == "active"

        await conn.execute("DELETE FROM stripe_customers WHERE id=$1", customer_id)
    finally:
        await conn.close()


async def test_webhook_events_unique_stripe_event_id_constraint():
    conn = await asyncpg.connect(DSN)
    try:
        event_id = uuid.uuid4()
        stripe_event_id = f"evt_pg_{uuid.uuid4()}"

        # Migration 0004 must have created webhook_events.
        await conn.execute(
            "INSERT INTO webhook_events (id, stripe_event_id, event_type, payload, created_at) "
            "VALUES ($1,$2,$3,$4,now())",
            event_id, stripe_event_id, "customer.subscription.updated", '{"id": "%s"}' % stripe_event_id,
        )

        got = await conn.fetchval(
            "SELECT event_type FROM webhook_events WHERE id=$1", event_id
        )
        assert got == "customer.subscription.updated"

        # DB-enforced UNIQUE(stripe_event_id): a second insert with the same id must fail.
        with pytest.raises(asyncpg.exceptions.UniqueViolationError):
            await conn.execute(
                "INSERT INTO webhook_events (id, stripe_event_id, event_type, payload, "
                "created_at) VALUES ($1,$2,$3,$4,now())",
                uuid.uuid4(), stripe_event_id, "customer.subscription.deleted", "{}",
            )

        await conn.execute("DELETE FROM webhook_events WHERE id=$1", event_id)
    finally:
        await conn.close()
