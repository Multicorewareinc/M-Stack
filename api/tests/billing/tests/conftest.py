"""Test helpers: build the app with an injected in-memory aiosqlite engine (so tests never
touch Postgres, mirrors admin-control-plane's injected-engine pattern), plus a fake JetStream
consumer harness (FakeMsg/_install_fake_nats, mirroring rate-limiter-tpm's/enricher's — no
live NATS).

Two aiosqlite gotchas that otherwise make every test fail:
- `StaticPool` keeps the single in-memory connection alive so the schema persists across the
  app's connections within a test.
- `TestClient` MUST be entered as a context manager (`with client:`) so the app's lifespan
  runs `create_all` on the injected engine — otherwise the tables don't exist.
"""

from __future__ import annotations

import json
import sys
import types

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from main import create_app
from settings import Settings


def make_engine():
    """A fresh in-memory aiosqlite engine whose single connection persists for the test."""
    return create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)


class FakeMsg:
    def __init__(self, event: dict) -> None:
        self.data = json.dumps(event).encode()
        self.acked = False

    async def ack(self) -> None:
        self.acked = True


def raw_msg(data: bytes) -> FakeMsg:
    msg = FakeMsg({})
    msg.data = data
    return msg


class StubConsumer:
    """Stand-in for BillingConsumer exposing only `active`, for ops/metrics tests."""

    def __init__(self, *, active: bool = True) -> None:
        self._active = active

    @property
    def active(self) -> bool:
        return self._active


async def make_consumer(*, engine=None, **settings_kwargs):
    """Build a BillingConsumer directly (async unit tests), wired to a fresh aiosqlite engine
    with the schema created. Returns (consumer, engine, sessionmaker)."""
    from consumer import BillingConsumer
    from db import Base, build_sessionmaker

    eng = engine or make_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = build_sessionmaker(eng)
    consumer = BillingConsumer(Settings(**settings_kwargs), sessionmaker)
    return consumer, eng, sessionmaker


def make_client(*, engine=None, consumer=None, reporter=None, **settings_kwargs):
    """Build the app with an injected aiosqlite engine (schema created via lifespan) and
    (optionally) fake consumer/reporter. Returns TestClient (enter as `with client:`)."""
    eng = engine or make_engine()
    app = create_app(Settings(**settings_kwargs), engine=eng, consumer=consumer, reporter=reporter)
    from fastapi.testclient import TestClient

    return TestClient(app, raise_server_exceptions=False)


class FakeSub:
    def __init__(self):
        self.unsubscribed = False

    async def unsubscribe(self):
        self.unsubscribed = True


class FakeJS:
    def __init__(self, *, subscribe_error=None):
        self._subscribe_error = subscribe_error
        self.subscribe_calls = 0

    async def add_stream(self, **kwargs):
        return None

    async def subscribe(self, *args, **kwargs):
        self.subscribe_calls += 1
        if self._subscribe_error is not None:
            raise self._subscribe_error
        return FakeSub()


class FakeNC:
    def __init__(self, *, connected=True, js=None):
        self.is_connected = connected
        self._js = js or FakeJS()

    def jetstream(self):
        return self._js

    async def drain(self):
        pass


def install_fake_nats(monkeypatch, *, nc=None, connect_error=None):
    mod = types.ModuleType("nats")
    captured = {}

    async def connect(url, **kwargs):
        captured.update(kwargs)
        if connect_error is not None:
            raise connect_error
        return nc

    mod.connect = connect
    js_api = types.ModuleType("nats.js.api")

    class DeliverPolicy:
        NEW = "new"

    class ConsumerConfig:
        def __init__(self, **kwargs):
            pass

    js_api.DeliverPolicy = DeliverPolicy
    js_api.ConsumerConfig = ConsumerConfig
    monkeypatch.setitem(sys.modules, "nats", mod)
    monkeypatch.setitem(sys.modules, "nats.js", types.ModuleType("nats.js"))
    monkeypatch.setitem(sys.modules, "nats.js.api", js_api)
    return captured


def response_event(**overrides) -> dict:
    """A canonical enriched `response` event."""
    ev = {
        "type": "response",
        "request_id": "req-1",
        "principal": "u1",
        "model": "m1",
        "status": 200,
        "usage": {"total_tokens": 150, "source": "provider"},
        "ts": 1234.5,
        "duration_ms": 12.0,
        "stream": False,
    }
    ev.update(overrides)
    return ev


def make_outbox_row(*, status="pending", attempts=0, next_attempt_at=None, request_id="req-out-1", payload=None):
    """Seed one Outbox row (import lazily to avoid a hard dependency for tests that don't
    touch the reporter)."""
    from models import Outbox

    return Outbox(
        request_id=request_id,
        payload=payload or {"request_id": request_id, "principal": "u1", "model": "m1", "total_tokens": 150, "source": "provider", "ts": 1234.5},
        status=status,
        attempts=attempts,
        next_attempt_at=next_attempt_at,
    )


def make_stripe_customer(*, principal="u1", stripe_customer_id="cus_existing"):
    """Seed one StripeCustomer row (import lazily, mirroring make_outbox_row's convention)."""
    from models import StripeCustomer

    return StripeCustomer(principal=principal, stripe_customer_id=stripe_customer_id)


class FakeStripeError(Exception):
    """A stand-in raised by the layer-1 fake raw Stripe method — real tests raise the actual
    `stripe.*` exception classes; this is only used where a test needs an exception whose
    constraint text is irrelevant."""


class Layer1FakeMethod:
    """Layer 1: stands in for the raw Stripe SDK method `report_to_stripe`/
    `create_or_get_stripe_customer`/`create_or_update_stripe_subscription` calls. Raises a
    given exception, or returns `returns` (a stand-in for the Stripe API response — e.g. a
    `types.SimpleNamespace(id="cus_new")`), so the caller's OWN try/except classification logic
    actually runs — this is what exercises the exception-to-result mapping, distinct from the
    layer-2 stubs below (which bypass that classification entirely). Accepts `*args` (not a
    fixed single `params`) since Stripe's own SDK shapes differ: `.create(params)` takes one
    positional argument, `.modify(id, params)` takes two — this fake stands in for either."""

    def __init__(self, *, raises: Exception | None = None, returns=None) -> None:
        self._raises = raises
        self._returns = returns
        self.calls: list[dict] = []

    def __call__(self, *args, options=None):
        self.calls.append({"args": args, "options": options})
        if self._raises is not None:
            raise self._raises
        return self._returns


class FakeStripeClient:
    """Wraps a Layer1FakeMethod behind the `client.billing.meter_events.create` shape
    `report_to_stripe` actually calls."""

    def __init__(self, method: Layer1FakeMethod) -> None:
        self.billing = types.SimpleNamespace(meter_events=types.SimpleNamespace(create=method))


class FakeStripeCustomerClient:
    """Wraps a Layer1FakeMethod behind the `client.customers.create` shape
    `create_or_get_stripe_customer` actually calls."""

    def __init__(self, method: Layer1FakeMethod) -> None:
        self.customers = types.SimpleNamespace(create=method)


class FakeStripeSubscriptionClient:
    """Wraps a Layer1FakeMethod behind the `client.subscriptions.create`/`.modify` shapes
    `create_or_update_stripe_subscription` calls — separate fake from
    FakeStripeCustomerClient since a single test may need both (the subscription endpoint
    resolves a customer first, per D2 of add-billing-plan-subscription-linkage)."""

    def __init__(self, create_method: Layer1FakeMethod, modify_method: Layer1FakeMethod | None = None) -> None:
        self.subscriptions = types.SimpleNamespace(
            create=create_method, modify=modify_method or create_method
        )


class FakeStripeEndpointClient:
    """A combined fake for the full internal endpoint (add-billing-plan-subscription-linkage),
    exposing `.customers.create`, `.subscriptions.create`, and `.subscriptions.modify` behind
    Layer1FakeMethod instances that default to succeeding (returning an object with a
    Stripe-shaped `.id`) — tests can override any one to raise or return a specific id."""

    def __init__(
        self, *,
        customer_method: Layer1FakeMethod | None = None,
        subscription_create_method: Layer1FakeMethod | None = None,
        subscription_modify_method: Layer1FakeMethod | None = None,
    ) -> None:
        self.customer_method = customer_method or Layer1FakeMethod(
            returns=types.SimpleNamespace(id="cus_endpoint")
        )
        self.subscription_create_method = subscription_create_method or Layer1FakeMethod(
            returns=types.SimpleNamespace(id="sub_endpoint")
        )
        self.subscription_modify_method = subscription_modify_method or Layer1FakeMethod(
            returns=types.SimpleNamespace(id="sub_endpoint")
        )
        self.customers = types.SimpleNamespace(create=self.customer_method)
        self.subscriptions = types.SimpleNamespace(
            create=self.subscription_create_method, modify=self.subscription_modify_method
        )


class StubReportFn:
    """Layer 2: stands in for `report_to_stripe` itself (injected as StripeReporter's
    `report_fn`), returning a `ReportResult` directly per call — bypassing classification
    entirely (already covered by layer 1) so `run_once()`'s outbox-lifecycle handling is
    tested independently. `results` is a list consumed in order; an entry may be a
    `ReportResult` string to return, or an `Exception` instance to RAISE on that call (used by
    the failure-isolation test). The last entry repeats once the list is exhausted."""

    def __init__(self, results: list) -> None:
        self._results = results
        self.calls: list[dict] = []

    async def __call__(self, client, *, event_name, principal, total_tokens, idempotency_key, customer_field):
        self.calls.append(
            {
                "event_name": event_name, "principal": principal, "total_tokens": total_tokens,
                "idempotency_key": idempotency_key, "customer_field": customer_field,
            }
        )
        index = min(len(self.calls) - 1, len(self._results) - 1)
        outcome = self._results[index]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class StubCustomerResolver:
    """Stand-in for `create_or_get_stripe_customer` (design D7 two-layer convention, extended to
    customer resolution by add-billing-stripe-customer-mapping): bypasses classification
    entirely, returning a `(CustomerResult, stripe_customer_id)` tuple per call — or raising, if
    an entry is an `Exception` instance. `results` is consumed in order; the last entry repeats
    once exhausted (mirrors `StubReportFn`'s convention exactly)."""

    def __init__(self, results: list) -> None:
        self._results = results
        self.calls: list[dict] = []

    async def __call__(self, client, session, *, principal):
        self.calls.append({"principal": principal})
        index = min(len(self.calls) - 1, len(self._results) - 1)
        outcome = self._results[index]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


async def make_reporter(*, engine=None, results=None, customer_results=None, **settings_kwargs):
    """Build a StripeReporter directly (async unit tests), wired to a fresh aiosqlite engine
    (schema created) AND a non-empty dummy stripe_secret_key (so the inert gate does not
    suppress the injected layer-2 stub — only the real-inert-gate test relies on the real,
    default-empty key with NO injection to prove the actual inert gate). A `customer_resolver`
    stub is ALWAYS injected too (default: every row's customer resolves as `"reused"`,
    `"cus_default"` — a no-op precondition tests that don't care about customer resolution can
    ignore entirely); pass `customer_results=[...]` to exercise customer-resolution outcomes
    specifically. Returns (reporter, engine, sessionmaker, stub) — the customer stub is reachable
    via `reporter._customer_resolver` when a test needs its `.calls`."""
    from db import Base, build_sessionmaker

    eng = engine or make_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = build_sessionmaker(eng)
    settings_kwargs.setdefault("stripe_secret_key", "sk_test_dummy")
    stub = StubReportFn(results if results is not None else ["sent"])
    customer_stub = StubCustomerResolver(
        customer_results if customer_results is not None else [("reused", "cus_default")]
    )
    from reporter import StripeReporter

    reporter = StripeReporter(
        Settings(**settings_kwargs), sessionmaker, report_fn=stub, customer_resolver=customer_stub
    )
    return reporter, eng, sessionmaker, stub


@pytest.fixture
def consumer_factory():
    return make_consumer


@pytest.fixture
def client_factory():
    return make_client


def make_subscriptions_app(*, engine=None, stripe_client=None, **settings_kwargs):
    """Build the app with `service_api_key` set, for `/internal/v1/subscriptions` tests
    (add-billing-plan-subscription-linkage). `stripe_client` overrides `app.state.stripe_client`
    directly post-construction — the endpoint reads that state, normally populated from
    `reporter.stripe_client`; overriding it directly avoids needing a real `STRIPE_SECRET_KEY`
    to get a non-None client wired in for tests. Returns (TestClient, engine)."""
    from fastapi.testclient import TestClient

    eng = engine or make_engine()
    settings_kwargs.setdefault("service_api_key", "test-billing-service-key")
    app = create_app(Settings(**settings_kwargs), engine=eng)
    if stripe_client is not None:
        app.state.stripe_client = stripe_client
    return TestClient(app, raise_server_exceptions=False), eng


def service_headers(key: str = "test-billing-service-key") -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def reporter_factory():
    return make_reporter
