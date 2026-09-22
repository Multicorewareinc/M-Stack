"""Covers specs/billing-usage-api/spec.md: GET /internal/v1/usage/summary and GET
/internal/v1/usage/timeseries — org-scoped totals/daily buckets over the `usage` ledger.

Rows are seeded directly via an async session (not replayed through the consumer/NATS) — this is
a read-path test, matching test_subscriptions.py's Layer-2 pattern (`make_subscriptions_app` +
`with client:` + `client.get(...)`). Every seeded row sets `ts`/`created_at` explicitly to a
fixed, test-controlled instant — never the model's real-wall-clock default — so no test here
depends on when it happens to run, including `test_summary_defaults_to_current_month`, which
additionally monkeypatches `router._now_utc` rather than relying on real wall-clock time (council
review fix: avoids flakiness near a UTC month boundary).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import router
from conftest import make_subscriptions_app, service_headers
from db import build_sessionmaker
from models import Usage

H = service_headers()

UTC = timezone.utc


def _dt(y, m, d, hour=12):
    return datetime(y, m, d, hour, tzinfo=UTC)


def _row(request_id, *, principal, total_tokens=100, prompt_tokens=60, completion_tokens=40,
          ts=None, created_at=None, source="provider", api_key_id=None, owner_id=None):
    return Usage(
        request_id=request_id,
        principal=principal,
        model="m1",
        total_tokens=total_tokens,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        source=source,
        ts=ts,
        created_at=created_at if created_at is not None else (ts or _dt(2026, 1, 1)),
        api_key_id=api_key_id,
        owner_id=owner_id,
    )


def _seed(engine, rows) -> None:
    async def _run():
        sessionmaker = build_sessionmaker(engine)
        async with sessionmaker() as s:
            s.add_all(rows)
            await s.commit()

    asyncio.run(_run())


# --- summary -------------------------------------------------------------------------------


def test_summary_totals_scoped_to_org_and_period():
    client, engine = make_subscriptions_app()
    with client:
        _seed(engine, [
            _row("r1", principal="org-A", total_tokens=100, prompt_tokens=60, completion_tokens=40, ts=_dt(2026, 1, 5)),
            _row("r2", principal="org-A", total_tokens=200, prompt_tokens=120, completion_tokens=80, ts=_dt(2026, 1, 10)),
            _row("r3", principal="org-A", total_tokens=300, prompt_tokens=180, completion_tokens=120, ts=_dt(2026, 1, 20)),
            _row("r4", principal="org-B", total_tokens=999, ts=_dt(2026, 1, 15)),
        ])
        r = client.get(
            "/internal/v1/usage/summary",
            params={"org_id": "org-A", "start": "2026-01-01", "end": "2026-02-01"},
            headers=H,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["requests"] == 3
        assert body["total_tokens"] == 600
        assert body["prompt_tokens"] == 360
        assert body["completion_tokens"] == 240


def test_summary_uncountable_row_counts_request_zero_tokens():
    client, engine = make_subscriptions_app()
    with client:
        _seed(engine, [
            _row("r1", principal="org-A", total_tokens=None, prompt_tokens=None,
                 completion_tokens=None, ts=_dt(2026, 1, 5), source="none"),
        ])
        r = client.get(
            "/internal/v1/usage/summary",
            params={"org_id": "org-A", "start": "2026-01-01", "end": "2026-02-01"},
            headers=H,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["requests"] == 1
        assert body["total_tokens"] == 0
        assert body["prompt_tokens"] == 0
        assert body["completion_tokens"] == 0


def test_summary_null_ts_uses_created_at():
    client, engine = make_subscriptions_app()
    with client:
        _seed(engine, [
            _row("r1", principal="org-A", total_tokens=50, ts=None, created_at=_dt(2026, 1, 12)),
        ])
        r = client.get(
            "/internal/v1/usage/summary",
            params={"org_id": "org-A", "start": "2026-01-01", "end": "2026-02-01"},
            headers=H,
        )
        assert r.status_code == 200
        assert r.json()["requests"] == 1
        assert r.json()["total_tokens"] == 50


def test_summary_empty_period_returns_zeros():
    client, engine = make_subscriptions_app()
    with client:
        r = client.get(
            "/internal/v1/usage/summary",
            params={"org_id": "org-A", "start": "2026-01-01", "end": "2026-02-01"},
            headers=H,
        )
        assert r.status_code == 200
        body = r.json()
        assert body == {
            "requests": 0, "total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "period": {"start": "2026-01-01", "end": "2026-02-01"},
        }


def test_summary_defaults_to_current_month(monkeypatch):
    monkeypatch.setattr(router, "_now_utc", lambda: _dt(2026, 6, 15))
    client, engine = make_subscriptions_app()
    with client:
        _seed(engine, [
            _row("r1", principal="org-A", total_tokens=10, ts=_dt(2026, 6, 3)),  # current month
            _row("r2", principal="org-A", total_tokens=99, ts=_dt(2026, 5, 20)),  # previous month
        ])
        r = client.get("/internal/v1/usage/summary", params={"org_id": "org-A"}, headers=H)
        assert r.status_code == 200
        body = r.json()
        assert body["requests"] == 1
        assert body["total_tokens"] == 10
        assert body["period"] == {"start": "2026-06-01", "end": "2026-07-01"}


def test_summary_requires_service_key():
    client, _ = make_subscriptions_app()
    with client:
        r = client.get("/internal/v1/usage/summary", params={"org_id": "org-A"})
        assert r.status_code == 401
        r = client.get(
            "/internal/v1/usage/summary",
            params={"org_id": "org-A"},
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert r.status_code == 401


def test_summary_invalid_period_is_rejected():
    client, engine = make_subscriptions_app()
    with client:
        _seed(engine, [_row("r1", principal="org-A", total_tokens=10, ts=_dt(2026, 1, 5))])
        r = client.get(
            "/internal/v1/usage/summary",
            params={"org_id": "org-A", "start": "2026-09-15", "end": "2026-09-01"},
            headers=H,
        )
        assert r.status_code == 422
        assert r.json()["error"]["type"] == "unprocessable"


# --- timeseries ----------------------------------------------------------------------------


def test_timeseries_zero_fills_days_without_usage():
    client, engine = make_subscriptions_app()
    with client:
        _seed(engine, [
            _row("r1", principal="org-A", total_tokens=50, prompt_tokens=30, completion_tokens=20, ts=_dt(2026, 1, 1)),
        ])
        r = client.get(
            "/internal/v1/usage/timeseries",
            params={"org_id": "org-A", "start": "2026-01-01", "end": "2026-01-06"},
            headers=H,
        )
        assert r.status_code == 200
        buckets = r.json()["buckets"]
        assert [b["date"] for b in buckets] == [
            "2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05",
        ]
        assert buckets[0] == {
            "date": "2026-01-01", "requests": 1, "total_tokens": 50,
            "prompt_tokens": 30, "completion_tokens": 20,
        }
        for b in buckets[1:]:
            assert b["requests"] == 0
            assert b["total_tokens"] == 0
            assert b["prompt_tokens"] == 0
            assert b["completion_tokens"] == 0


def test_timeseries_null_ts_bucketed_by_created_at():
    client, engine = make_subscriptions_app()
    with client:
        _seed(engine, [
            _row("r1", principal="org-A", total_tokens=15, ts=None, created_at=_dt(2026, 1, 3)),
        ])
        r = client.get(
            "/internal/v1/usage/timeseries",
            params={"org_id": "org-A", "start": "2026-01-01", "end": "2026-01-06"},
            headers=H,
        )
        assert r.status_code == 200
        buckets = {b["date"]: b for b in r.json()["buckets"]}
        assert buckets["2026-01-03"]["requests"] == 1
        assert buckets["2026-01-03"]["total_tokens"] == 15


def test_timeseries_sums_reconcile_with_summary():
    client, engine = make_subscriptions_app()
    with client:
        _seed(engine, [
            _row("r1", principal="org-A", total_tokens=100, prompt_tokens=60, completion_tokens=40, ts=_dt(2026, 1, 1)),
            _row("r2", principal="org-A", total_tokens=200, prompt_tokens=120, completion_tokens=80, ts=_dt(2026, 1, 3)),
            _row("r3", principal="org-A", total_tokens=None, prompt_tokens=None, completion_tokens=None,
                 ts=_dt(2026, 1, 5), source="none"),
        ])
        params = {"org_id": "org-A", "start": "2026-01-01", "end": "2026-01-08"}
        summary = client.get("/internal/v1/usage/summary", params=params, headers=H).json()
        timeseries = client.get("/internal/v1/usage/timeseries", params=params, headers=H).json()

        buckets = timeseries["buckets"]
        assert sum(b["requests"] for b in buckets) == summary["requests"]
        assert sum(b["total_tokens"] for b in buckets) == summary["total_tokens"]
        assert sum(b["prompt_tokens"] for b in buckets) == summary["prompt_tokens"]
        assert sum(b["completion_tokens"] for b in buckets) == summary["completion_tokens"]


def test_timeseries_requires_service_key():
    client, _ = make_subscriptions_app()
    with client:
        r = client.get("/internal/v1/usage/timeseries", params={"org_id": "org-A"})
        assert r.status_code == 401
        r = client.get(
            "/internal/v1/usage/timeseries",
            params={"org_id": "org-A"},
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert r.status_code == 401


# --- by-user (add-billing-usage-breakdown-api) ---------------------------------------------

_P = {"org_id": "org-A", "start": "2026-01-01", "end": "2026-02-01"}


def test_by_user_groups_with_null_bucket():
    client, engine = make_subscriptions_app()
    with client:
        _seed(engine, [
            _row("r1", principal="org-A", total_tokens=100, owner_id="u1", ts=_dt(2026, 1, 5)),
            _row("r2", principal="org-A", total_tokens=200, owner_id="u1", ts=_dt(2026, 1, 6)),
            _row("r3", principal="org-A", total_tokens=300, owner_id="u2", ts=_dt(2026, 1, 7)),
            _row("r4", principal="org-A", total_tokens=50, owner_id=None, ts=_dt(2026, 1, 8)),
        ])
        r = client.get("/internal/v1/usage/by-user", params=_P, headers=H)
        assert r.status_code == 200
        users = r.json()["users"]
        by_id = {u["owner_id"]: u for u in users}
        assert set(by_id) == {"u1", "u2", None}
        assert by_id["u1"]["requests"] == 2 and by_id["u1"]["total_tokens"] == 300
        assert by_id["u2"]["requests"] == 1 and by_id["u2"]["total_tokens"] == 300
        assert by_id[None]["requests"] == 1 and by_id[None]["total_tokens"] == 50


def test_by_user_sums_reconcile_with_summary():
    client, engine = make_subscriptions_app()
    with client:
        _seed(engine, [
            _row("r1", principal="org-A", total_tokens=100, prompt_tokens=60, completion_tokens=40, owner_id="u1", ts=_dt(2026, 1, 5)),
            _row("r2", principal="org-A", total_tokens=200, prompt_tokens=120, completion_tokens=80, owner_id="u2", ts=_dt(2026, 1, 6)),
            _row("r3", principal="org-A", total_tokens=None, prompt_tokens=None, completion_tokens=None, owner_id=None, ts=_dt(2026, 1, 7), source="none"),
        ])
        summary = client.get("/internal/v1/usage/summary", params=_P, headers=H).json()
        users = client.get("/internal/v1/usage/by-user", params=_P, headers=H).json()["users"]
        for field in ("requests", "total_tokens", "prompt_tokens", "completion_tokens"):
            assert sum(u[field] for u in users) == summary[field]


def test_by_user_empty_period_returns_empty_list():
    client, _ = make_subscriptions_app()
    with client:
        r = client.get("/internal/v1/usage/by-user", params=_P, headers=H)
        assert r.status_code == 200
        assert r.json()["users"] == []


def test_by_user_requires_service_key():
    client, _ = make_subscriptions_app()
    with client:
        assert client.get("/internal/v1/usage/by-user", params={"org_id": "org-A"}).status_code == 401
        r = client.get(
            "/internal/v1/usage/by-user",
            params={"org_id": "org-A"},
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert r.status_code == 401


# --- by-key (add-billing-usage-breakdown-api) ----------------------------------------------


def test_by_key_groups_with_null_bucket():
    client, engine = make_subscriptions_app()
    with client:
        _seed(engine, [
            _row("r1", principal="org-A", total_tokens=100, api_key_id="k1", ts=_dt(2026, 1, 5)),
            _row("r2", principal="org-A", total_tokens=300, api_key_id="k2", ts=_dt(2026, 1, 6)),
            _row("r3", principal="org-A", total_tokens=50, api_key_id=None, ts=_dt(2026, 1, 7)),
        ])
        r = client.get("/internal/v1/usage/by-key", params=_P, headers=H)
        assert r.status_code == 200
        by_id = {k["api_key_id"]: k for k in r.json()["api_keys"]}
        assert set(by_id) == {"k1", "k2", None}
        assert by_id["k1"]["total_tokens"] == 100
        assert by_id["k2"]["total_tokens"] == 300
        assert by_id[None]["total_tokens"] == 50


def test_by_key_sums_reconcile_with_summary():
    client, engine = make_subscriptions_app()
    with client:
        _seed(engine, [
            _row("r1", principal="org-A", total_tokens=100, prompt_tokens=60, completion_tokens=40, api_key_id="k1", ts=_dt(2026, 1, 5)),
            _row("r2", principal="org-A", total_tokens=200, prompt_tokens=120, completion_tokens=80, api_key_id="k2", ts=_dt(2026, 1, 6)),
            _row("r3", principal="org-A", total_tokens=None, prompt_tokens=None, completion_tokens=None, api_key_id=None, ts=_dt(2026, 1, 7), source="none"),
        ])
        summary = client.get("/internal/v1/usage/summary", params=_P, headers=H).json()
        keys = client.get("/internal/v1/usage/by-key", params=_P, headers=H).json()["api_keys"]
        for field in ("requests", "total_tokens", "prompt_tokens", "completion_tokens"):
            assert sum(k[field] for k in keys) == summary[field]


def test_by_key_requires_service_key():
    client, _ = make_subscriptions_app()
    with client:
        assert client.get("/internal/v1/usage/by-key", params={"org_id": "org-A"}).status_code == 401
        r = client.get(
            "/internal/v1/usage/by-key",
            params={"org_id": "org-A"},
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert r.status_code == 401
