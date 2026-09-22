"""Covers specs/billing-stripe-reporting/spec.md: interval-based draining, a fresh idempotency
key per attempt, the outbox lifecycle (sent / transient-backoff / terminal-failed), the inert-by-
default gate, observability, and poll-loop failure isolation. Two independent test layers
(design D7): layer 1 exercises `report_to_stripe`'s own Stripe-exception classification in
isolation; layer 2 exercises `StripeReporter.run_once()`'s outbox-lifecycle handling via an
injected `report_fn` stub that bypasses classification entirely."""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from conftest import FakeStripeClient, Layer1FakeMethod, make_outbox_row
from models import Outbox
from reporter import report_to_stripe


# --- Layer 1: report_to_stripe's own exception classification (5.1a) -------------------------
async def test_report_to_stripe_classifies_exceptions_correctly():
    import stripe

    cases = [
        (None, "sent"),
        (stripe.APIConnectionError("net"), "transient_failure"),
        (stripe.RateLimitError("429"), "transient_failure"),  # must precede InvalidRequestError
        (stripe.APIError("5xx"), "transient_failure"),
        (stripe.InvalidRequestError("bad request", param=None), "permanent_failure"),
        (stripe.AuthenticationError("bad key"), "permanent_failure"),
    ]
    for exc, expected in cases:
        method = Layer1FakeMethod(raises=exc)
        client = FakeStripeClient(method)
        result = await report_to_stripe(
            client, event_name="tokens_used", principal="org-1", total_tokens=150,
            idempotency_key="billing:req-1",
        )
        assert result == expected, f"{exc!r} -> expected {expected}, got {result}"
        assert len(method.calls) == 1
        assert method.calls[0]["options"] == {"idempotency_key": "billing:req-1"}


async def _seed(sessionmaker, row) -> None:
    async with sessionmaker() as s:
        s.add(row)
        await s.commit()


async def _get(sessionmaker, request_id: str) -> Outbox:
    async with sessionmaker() as s:
        return (await s.scalars(select(Outbox).where(Outbox.request_id == request_id))).one()


# --- Layer 2: reporter lifecycle (5.2-5.10) ---------------------------------------------------
async def test_due_row_is_reported_and_marked_sent(reporter_factory):
    reporter, _eng, sessionmaker, stub = await reporter_factory(results=["sent"])
    await _seed(sessionmaker, make_outbox_row(request_id="req-a"))

    await reporter.run_once()

    row = await _get(sessionmaker, "req-a")
    assert row.status == "sent"
    assert row.sent_at is not None
    assert len(stub.calls) == 1


async def test_sent_row_is_not_re_attempted(reporter_factory):
    reporter, _eng, sessionmaker, stub = await reporter_factory(results=["sent"])
    await _seed(sessionmaker, make_outbox_row(request_id="req-b"))

    await reporter.run_once()
    await reporter.run_once()  # second cycle

    assert len(stub.calls) == 1  # not called again
    row = await _get(sessionmaker, "req-b")
    assert row.status == "sent"


async def test_not_yet_due_row_is_skipped(reporter_factory):
    import datetime

    reporter, _eng, sessionmaker, stub = await reporter_factory(results=["sent"])
    future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
    await _seed(sessionmaker, make_outbox_row(request_id="req-c", next_attempt_at=future))

    await reporter.run_once()

    assert len(stub.calls) == 0
    row = await _get(sessionmaker, "req-c")
    assert row.status == "pending"


async def test_transient_failure_backs_off_and_stays_pending(reporter_factory):
    reporter, _eng, sessionmaker, stub = await reporter_factory(
        results=["transient_failure"], reporter_max_attempts=5
    )
    await _seed(sessionmaker, make_outbox_row(request_id="req-d"))

    await reporter.run_once()

    row = await _get(sessionmaker, "req-d")
    assert row.status == "pending"
    assert row.attempts == 1
    assert row.next_attempt_at is not None


async def test_ceiling_reached_marks_row_failed_observably(reporter_factory, caplog):
    reporter, _eng, sessionmaker, stub = await reporter_factory(
        results=["transient_failure"],
        reporter_max_attempts=2,
        reporter_backoff_base_seconds=0,
        reporter_backoff_max_seconds=0,
    )
    await _seed(sessionmaker, make_outbox_row(request_id="req-e"))

    with caplog.at_level("ERROR"):
        await reporter.run_once()
        await reporter.run_once()

    row = await _get(sessionmaker, "req-e")
    assert row.status == "failed"
    assert row.attempts == 2
    # error logged naming the request_id (via `extra={"request_id": ...}`)
    assert any(getattr(r, "request_id", None) == "req-e" for r in caplog.records)
    # row still exists (queryable, not deleted) — confirmed by the successful _get() above


async def test_permanent_failure_fails_fast_without_retry(reporter_factory):
    reporter, _eng, sessionmaker, stub = await reporter_factory(results=["permanent_failure"])
    await _seed(sessionmaker, make_outbox_row(request_id="req-f"))

    await reporter.run_once()

    row = await _get(sessionmaker, "req-f")
    assert row.status == "failed"
    assert row.attempts == 1  # bumped before the call (for keying), even though it's terminal
    # and no retry gets scheduled — status went straight to "failed", not "pending" + backoff.


async def test_idempotency_key_changes_across_retries(reporter_factory):
    # `attempts` is folded into the idempotency key, so each actual attempt — including an
    # automatic retry after a transient failure — gets its own key rather than reusing one.
    reporter, _eng, sessionmaker, stub = await reporter_factory(
        results=["transient_failure", "sent"],
        reporter_backoff_base_seconds=0,
        reporter_backoff_max_seconds=0,
    )
    await _seed(sessionmaker, make_outbox_row(request_id="req-g"))

    await reporter.run_once()
    await reporter.run_once()

    assert len(stub.calls) == 2
    key1 = stub.calls[0]["idempotency_key"]
    key2 = stub.calls[1]["idempotency_key"]
    assert key1 != key2
    assert key1 == "billing:req-g:1"
    assert key2 == "billing:req-g:2"


async def test_inert_when_stripe_key_unset_via_real_create_app():
    """The REAL inert gate, not a re-test of stub injection (task 5.8): create_app's owns-it
    path, default Settings() (empty stripe_secret_key), no `reporter=` override. Calls
    create_app directly (no TestClient/lifespan needed — the reporter object is fully built
    before the lifespan even exists) so there is no event-loop nesting to manage."""
    from conftest import make_engine
    from db import Base, build_sessionmaker
    from main import create_app
    from settings import Settings

    eng = make_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = build_sessionmaker(eng)

    app = create_app(Settings(), engine=eng)
    reporter = app.state.reporter
    assert reporter is not None  # unconditionally constructed (D5) — this is what makes the
    # gate provable at all: a None reporter would give nothing to call run_once() on

    await _seed(sessionmaker, make_outbox_row(request_id="req-h"))
    await reporter.run_once()

    row = await _get(sessionmaker, "req-h")
    assert row.status == "pending"
    assert row.attempts == 0


async def test_metrics_exposes_reporter_outcomes_and_gauge_values():
    """Drives real sent/transient/terminal outcomes through the app's OWN reporter instance
    (injecting a stub report_fn onto it post-construction), then reads /metrics via a plain
    ASGI call — avoiding TestClient's lifespan/portal entirely so this stays a single,
    straightforward async test."""
    import re

    from conftest import StubCustomerResolver, StubReportFn, make_engine, make_outbox_row as _mkrow
    from db import Base, build_sessionmaker
    from main import create_app
    from settings import Settings

    eng = make_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = build_sessionmaker(eng)

    app = create_app(Settings(reporter_max_attempts=1), engine=eng)
    reporter = app.state.reporter
    reporter._stripe_client = object()  # non-None so run_once() does not short-circuit inert
    reporter._report_fn = StubReportFn(["sent", "transient_failure", "permanent_failure"])
    # Every row's customer resolves as a no-op precondition — this test only exercises
    # usage-reporting outcomes, not customer-resolution outcomes (see test_customer_mapping.py).
    reporter._customer_resolver = StubCustomerResolver([("reused", "cus_default")])

    await _seed(sessionmaker, _mkrow(request_id="m1"))
    await _seed(sessionmaker, _mkrow(request_id="m2"))
    await _seed(sessionmaker, _mkrow(request_id="m3"))
    await reporter.run_once()  # processes all three due rows in one cycle

    await reporter.start()
    from prometheus_client import generate_latest

    text = generate_latest().decode()
    await reporter.stop()
    text_after_stop = generate_latest().decode()

    def _value(t: str, name: str, **labels) -> float:
        for line in t.splitlines():
            if not line.startswith(name + "{"):
                continue
            if all(f'{k}="{v}"' in line for k, v in labels.items()):
                return float(line.rsplit(" ", 1)[1])
        return 0.0

    assert _value(text, "billing_stripe_reported_total", outcome="sent") >= 1
    assert _value(text, "billing_stripe_reported_total", outcome="transient_failure") >= 1
    assert _value(text, "billing_stripe_reported_total", outcome="terminal_failure") >= 1
    assert re.search(r"^billing_reporter_active\s+1\.0$", text, re.MULTILINE)
    assert re.search(r"^billing_reporter_active\s+0\.0$", text_after_stop, re.MULTILINE)


async def test_run_forever_survives_a_cycle_exception(reporter_factory):
    reporter, _eng, sessionmaker, stub = await reporter_factory(results=["sent"])
    reporter._settings.reporter_poll_interval_seconds = 0

    calls = {"n": 0}

    async def flaky_run_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")

    reporter.run_once = flaky_run_once  # type: ignore[assignment]

    task = asyncio.create_task(reporter.run_forever())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert calls["n"] >= 2  # the loop survived the first cycle's exception
