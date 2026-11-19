"""Covers specs/billing-metering/spec.md: countable/un-countable persistence, outbox payload
content, idempotency via the request_id UNIQUE constraint (narrowed IntegrityError handling —
NOT any integrity failure), database-fault ack safety, and non-response/malformed events.
Offline — injected in-memory aiosqlite engine, no live NATS/Postgres."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import pytest

from conftest import response_event
from consumer import _PersistError
from models import Outbox, Usage


def _fake_integrity_error(constraint_text: str) -> IntegrityError:
    return IntegrityError("INSERT ...", {}, Exception(constraint_text))


class FailingSession:
    """A session-shaped fake whose flush()/commit() raises on demand, so a DB fault can be
    simulated without a real broken connection."""

    def __init__(self, *, fail_on: str = "flush", exc: Exception | None = None) -> None:
        self.fail_on = fail_on
        self.exc = exc or RuntimeError("db down")
        self.rolled_back = False
        self.closed = False

    def add(self, obj) -> None:
        pass

    async def flush(self) -> None:
        if self.fail_on == "flush":
            raise self.exc

    async def commit(self) -> None:
        if self.fail_on == "commit":
            raise self.exc

    async def rollback(self) -> None:
        self.rolled_back = True

    async def close(self) -> None:
        self.closed = True


class FailingSessionmaker:
    def __init__(self, *, fail_on: str = "flush", exc: Exception | None = None) -> None:
        self.fail_on = fail_on
        self.exc = exc
        self.sessions: list[FailingSession] = []

    def __call__(self) -> FailingSession:
        s = FailingSession(fail_on=self.fail_on, exc=self.exc)
        self.sessions.append(s)
        return s


async def _count(sessionmaker, model) -> int:
    async with sessionmaker() as s:
        result = await s.scalars(select(model))
        return len(list(result))


# --- Countable event -> usage + pending outbox + ack (7.2) -----------------------------------
async def test_countable_event_persists_usage_and_outbox_and_acks(consumer_factory):
    consumer, _engine, sessionmaker = await consumer_factory()
    msg = response_event(request_id="req-a", usage={"total_tokens": 150, "source": "provider"})
    await consumer._on_msg(msg)

    async with sessionmaker() as s:
        usage = (await s.scalars(select(Usage).where(Usage.request_id == "req-a"))).one()
        assert usage.total_tokens == 150
        outbox = (await s.scalars(select(Outbox).where(Outbox.request_id == "req-a"))).one()
        assert outbox.status == "pending"


# --- Provider breakdown persisted (7.3) -------------------------------------------------------
async def test_provider_breakdown_persisted(consumer_factory):
    consumer, _engine, sessionmaker = await consumer_factory()
    event = response_event(
        request_id="req-b",
        usage={"total_tokens": 150, "source": "provider", "prompt_tokens": 100, "completion_tokens": 50},
    )
    await consumer._on_msg(event)
    async with sessionmaker() as s:
        usage = (await s.scalars(select(Usage).where(Usage.request_id == "req-b"))).one()
        assert usage.prompt_tokens == 100
        assert usage.completion_tokens == 50


# --- Outbox payload content is self-sufficient (7.10) -----------------------------------------
async def test_outbox_payload_content(consumer_factory):
    consumer, _engine, sessionmaker = await consumer_factory()
    event = response_event(
        request_id="req-9", principal="u1", model="m1",
        usage={"total_tokens": 150, "source": "provider"}, ts=1234.5,
    )
    await consumer._on_msg(event)
    async with sessionmaker() as s:
        outbox = (await s.scalars(select(Outbox).where(Outbox.request_id == "req-9"))).one()
        assert outbox.payload == {
            "request_id": "req-9", "principal": "u1", "model": "m1",
            "total_tokens": 150, "source": "provider", "ts": 1234.5,
        }


# --- Un-countable event audited, no outbox, acked (7.4) ---------------------------------------
async def test_uncountable_event_audited_no_outbox_and_acks(consumer_factory):
    consumer, _engine, sessionmaker = await consumer_factory()
    event = response_event(request_id="req-c", usage={"total_tokens": None, "source": "none", "reason": "no_body"})
    msg = event
    await consumer._on_msg(msg)

    async with sessionmaker() as s:
        usage = (await s.scalars(select(Usage).where(Usage.request_id == "req-c"))).one()
        assert usage.total_tokens is None
        outbox_count = len(list(await s.scalars(select(Outbox).where(Outbox.request_id == "req-c"))))
        assert outbox_count == 0


# --- Redelivery is a no-op via the REAL request_id UNIQUE constraint (7.5) ---------------------
async def test_redelivery_is_a_noop_via_unique_constraint(consumer_factory):
    consumer, _engine, sessionmaker = await consumer_factory()
    event = response_event(request_id="req-dup", usage={"total_tokens": 42, "source": "provider"})
    msg1 = event
    msg2 = event  # redelivery: same request_id
    await consumer._on_msg(msg1)
    await consumer._on_msg(msg2)

    assert await _count(sessionmaker, Usage) == 1
    assert await _count(sessionmaker, Outbox) == 1


# --- Generic (non-IntegrityError) DB fault -> not acked (7.6) ----------------------------------
async def test_generic_db_fault_is_not_acked():
    from consumer import BillingConsumer
    from settings import Settings

    failing = FailingSessionmaker(fail_on="commit", exc=RuntimeError("connection dropped"))
    consumer = BillingConsumer(Settings(), failing)
    msg = response_event(request_id="req-fault")
    # Withholding the ack is now expressed by raising: AckMode.ON_SUCCESS leaves the
    # message unacknowledged so the backbone redelivers it.
    with pytest.raises(_PersistError):
        await consumer._on_msg(msg)
    assert failing.sessions[0].rolled_back is True
    assert failing.sessions[0].closed is True


# --- A NON-request_id IntegrityError is NOT swallowed as a redelivery no-op (7.6b) -------------
async def test_non_request_id_integrity_error_is_not_swallowed():
    from consumer import BillingConsumer
    from settings import Settings

    failing = FailingSessionmaker(fail_on="flush", exc=_fake_integrity_error("uq_something_unrelated"))
    consumer = BillingConsumer(Settings(), failing)
    msg = response_event(request_id="req-other-constraint")
    with pytest.raises(_PersistError):  # NOT treated as an already-processed no-op
        await consumer._on_msg(msg)

    assert failing.sessions[0].rolled_back is True


# --- Non-response messages are ignored (7.7) ---------------------------------------------------
async def test_non_response_events_ignored(consumer_factory):
    """A non-`response` event persists nothing and returns normally, so the SDK acks it.

    Un-parseable payloads no longer reach this handler at all: the multistack.queue driver
    deserializes and, on malformed JSON, logs and acks before dispatch (redelivery cannot fix
    malformed JSON) — the same poison-message policy this consumer used to implement itself."""
    consumer, _engine, sessionmaker = await consumer_factory()
    await consumer._on_msg({"type": "request", "request_id": "r"})
    assert await _count(sessionmaker, Usage) == 0


# --- Malformed usage: non-numeric total, source not "none" -> treated as uncountable (7.15) ----
async def test_malformed_non_numeric_total_tokens_falls_through_to_uncountable(consumer_factory):
    consumer, _engine, sessionmaker = await consumer_factory()
    event = response_event(request_id="req-malformed", usage={"total_tokens": "not-a-number", "source": "provider"})
    msg = event
    await consumer._on_msg(msg)

    async with sessionmaker() as s:
        usage = (await s.scalars(select(Usage).where(Usage.request_id == "req-malformed"))).one()
        assert usage.total_tokens is None
        assert await _count(sessionmaker, Outbox) == 0


async def test_missing_usage_key_falls_through_to_uncountable(consumer_factory):
    consumer, _engine, sessionmaker = await consumer_factory()
    event = response_event(request_id="req-nousage")
    del event["usage"]
    await consumer._on_msg(event)
    async with sessionmaker() as s:
        usage = (await s.scalars(select(Usage).where(Usage.request_id == "req-nousage"))).one()
        assert usage.total_tokens is None


# --- A NOT NULL violation on request_id is NOT mistaken for the UNIQUE no-op -------------------
async def test_missing_request_id_is_not_swallowed_as_a_noop(consumer_factory):
    """A NOT NULL violation on `usage.request_id` (event lacks a request_id entirely) produces
    error text that ALSO contains the substring "request_id" — the same substring the UNIQUE
    no-op check looks for. `_is_request_id_violation` must require BOTH "unique" and
    "request_id" so this genuine data-corruption case is never silently acked as if it were an
    already-processed redelivery."""
    consumer, _engine, sessionmaker = await consumer_factory(rate_limits='{"user_default":1000}')
    event = response_event(principal="u1", model="m1")
    del event["request_id"]  # triggers a real NOT NULL violation, not a UNIQUE one
    msg = event
    with pytest.raises(_PersistError):  # a genuine fault — must redeliver, not be swallowed
        await consumer._on_msg(msg)
    async with sessionmaker() as s:
        rows = list(await s.scalars(select(Usage)))
        assert rows == []  # nothing committed


# --- Per-request attribution (add-billing-usage-attribution) ----------------------------------
async def test_attribution_persisted_when_present(consumer_factory):
    consumer, _engine, sessionmaker = await consumer_factory()
    event = response_event(
        request_id="req-attr", api_key_id="key-1", owner_id="owner-1",
        usage={"total_tokens": 150, "source": "provider"},
    )
    await consumer._on_msg(event)
    async with sessionmaker() as s:
        usage = (await s.scalars(select(Usage).where(Usage.request_id == "req-attr"))).one()
        assert usage.api_key_id == "key-1"
        assert usage.owner_id == "owner-1"


async def test_attribution_null_when_absent(consumer_factory):
    consumer, _engine, sessionmaker = await consumer_factory()
    # response_event() carries no api_key_id/owner_id by default (pre-attribution / replayed event).
    await consumer._on_msg(response_event(request_id="req-noattr"))
    async with sessionmaker() as s:
        usage = (await s.scalars(select(Usage).where(Usage.request_id == "req-noattr"))).one()
        assert usage.api_key_id is None
        assert usage.owner_id is None


async def test_attribution_does_not_disturb_idempotency_or_outbox(consumer_factory):
    consumer, _engine, sessionmaker = await consumer_factory()
    event = response_event(
        request_id="req-attr-dup", principal="u1", model="m1",
        api_key_id="key-1", owner_id="owner-1",
        usage={"total_tokens": 150, "source": "provider"}, ts=1234.5,
    )
    await consumer._on_msg(event)
    await consumer._on_msg(event)  # redelivery: same request_id

    assert await _count(sessionmaker, Usage) == 1
    assert await _count(sessionmaker, Outbox) == 1
    async with sessionmaker() as s:
        outbox = (await s.scalars(select(Outbox).where(Outbox.request_id == "req-attr-dup"))).one()
        # The outbox payload feeds the org-level Stripe reporter — attribution must NOT leak into it.
        assert outbox.payload == {
            "request_id": "req-attr-dup", "principal": "u1", "model": "m1",
            "total_tokens": 150, "source": "provider", "ts": 1234.5,
        }
        assert "api_key_id" not in outbox.payload
        assert "owner_id" not in outbox.payload
