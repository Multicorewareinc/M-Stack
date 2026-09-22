"""Postgres integration test for GET /internal/v1/usage/{summary,timeseries}'s query layer
(add-billing-usage-summary-api). Mirrors test_billing_pg.py's preconditions and skip discipline
(BILLING_PG_DSN set + `alembic upgrade head` already applied), but exercises `usage.py`'s
SQLAlchemy functions directly through a real asyncpg-backed async engine — this is what proves
`func.coalesce(Usage.ts, Usage.created_at)` behaves identically to the aiosqlite offline path
(design D1's portability claim), not just that the table/columns exist.

Run: `python -m pytest api/tests/billing/integration -q` with BILLING_PG_DSN set.
Skips (does not fail) when BILLING_PG_DSN is absent, so it never breaks the offline suite.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest

asyncpg = pytest.importorskip("asyncpg")

DSN = os.environ.get("BILLING_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="BILLING_PG_DSN not set — Postgres integration only")

UTC = timezone.utc


def _sqlalchemy_dsn(dsn: str) -> str:
    """BILLING_PG_DSN is a plain asyncpg DSN (postgresql://...); usage.py's SQLAlchemy functions
    need the +asyncpg driver prefix."""
    if "+asyncpg" in dsn:
        return dsn
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


def _dt(y, m, d, hour=12):
    return datetime(y, m, d, hour, tzinfo=UTC)


async def _make_sessionmaker():
    from sqlalchemy.ext.asyncio import create_async_engine

    from db import build_sessionmaker

    engine = create_async_engine(_sqlalchemy_dsn(DSN))
    return engine, build_sessionmaker(engine)


async def test_summary_org_scoping_and_default_coalesce_pg():
    from models import Usage
    from usage import get_usage_summary

    engine, sessionmaker = await _make_sessionmaker()
    org_a = f"pg-usage-org-a-{uuid.uuid4()}"
    org_b = f"pg-usage-org-b-{uuid.uuid4()}"
    try:
        async with sessionmaker() as s:
            s.add_all([
                Usage(
                    request_id=f"pg-usage-{uuid.uuid4()}", principal=org_a, model="m1",
                    total_tokens=100, prompt_tokens=60, completion_tokens=40, source="provider",
                    ts=_dt(2026, 1, 10), created_at=_dt(2026, 1, 10),
                ),
                Usage(
                    request_id=f"pg-usage-{uuid.uuid4()}", principal=org_a, model="m1",
                    total_tokens=200, prompt_tokens=120, completion_tokens=80, source="provider",
                    ts=_dt(2026, 1, 15), created_at=_dt(2026, 1, 15),
                ),
                Usage(
                    request_id=f"pg-usage-{uuid.uuid4()}", principal=org_b, model="m1",
                    total_tokens=999, source="provider",
                    ts=_dt(2026, 1, 12), created_at=_dt(2026, 1, 12),
                ),
            ])
            await s.commit()

        async with sessionmaker() as s:
            result = await get_usage_summary(s, org_a, _dt(2026, 1, 1), _dt(2026, 2, 1))
        assert result.requests == 2
        assert result.total_tokens == 300
        assert result.prompt_tokens == 180
        assert result.completion_tokens == 120
    finally:
        async with sessionmaker() as s:
            from sqlalchemy import delete

            await s.execute(delete(Usage).where(Usage.principal.in_([org_a, org_b])))
            await s.commit()
        await engine.dispose()


async def test_summary_null_ts_uses_created_at_pg():
    from models import Usage
    from usage import get_usage_summary

    engine, sessionmaker = await _make_sessionmaker()
    org = f"pg-usage-nullts-{uuid.uuid4()}"
    try:
        async with sessionmaker() as s:
            s.add(Usage(
                request_id=f"pg-usage-{uuid.uuid4()}", principal=org, model="m1",
                total_tokens=42, source="provider", ts=None, created_at=_dt(2026, 1, 20),
            ))
            await s.commit()

        async with sessionmaker() as s:
            result = await get_usage_summary(s, org, _dt(2026, 1, 1), _dt(2026, 2, 1))
        assert result.requests == 1
        assert result.total_tokens == 42
    finally:
        async with sessionmaker() as s:
            from sqlalchemy import delete

            await s.execute(delete(Usage).where(Usage.principal == org))
            await s.commit()
        await engine.dispose()


async def test_timeseries_zero_fills_and_reconciles_with_summary_pg():
    from models import Usage
    from usage import get_usage_summary, get_usage_timeseries

    engine, sessionmaker = await _make_sessionmaker()
    org = f"pg-usage-ts-{uuid.uuid4()}"
    try:
        async with sessionmaker() as s:
            s.add(Usage(
                request_id=f"pg-usage-{uuid.uuid4()}", principal=org, model="m1",
                total_tokens=50, prompt_tokens=30, completion_tokens=20, source="provider",
                ts=_dt(2026, 3, 1), created_at=_dt(2026, 3, 1),
            ))
            await s.commit()

        start, end = _dt(2026, 3, 1), _dt(2026, 3, 6)
        async with sessionmaker() as s:
            timeseries = await get_usage_timeseries(s, org, start, end)
        async with sessionmaker() as s:
            summary = await get_usage_summary(s, org, start, end)

        assert [b.date.isoformat() for b in timeseries.buckets] == [
            "2026-03-01", "2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05",
        ]
        assert sum(b.requests for b in timeseries.buckets) == summary.requests
        assert sum(b.total_tokens for b in timeseries.buckets) == summary.total_tokens
    finally:
        async with sessionmaker() as s:
            from sqlalchemy import delete

            await s.execute(delete(Usage).where(Usage.principal == org))
            await s.commit()
        await engine.dispose()


async def test_by_user_and_by_key_group_with_null_bucket_and_reconcile_pg():
    # Proves the grouped query (incl. the NULL bucket) works over real asyncpg and reconciles
    # with the summary total (add-billing-usage-breakdown-api).
    from models import Usage
    from usage import get_usage_by_key, get_usage_by_user, get_usage_summary

    engine, sessionmaker = await _make_sessionmaker()
    org = f"pg-usage-grp-{uuid.uuid4()}"
    try:
        async with sessionmaker() as s:
            s.add_all([
                Usage(request_id=f"pg-usage-{uuid.uuid4()}", principal=org, model="m1",
                      total_tokens=100, source="provider", owner_id="u1", api_key_id="k1",
                      ts=_dt(2026, 4, 2), created_at=_dt(2026, 4, 2)),
                Usage(request_id=f"pg-usage-{uuid.uuid4()}", principal=org, model="m1",
                      total_tokens=200, source="provider", owner_id="u1", api_key_id="k2",
                      ts=_dt(2026, 4, 3), created_at=_dt(2026, 4, 3)),
                Usage(request_id=f"pg-usage-{uuid.uuid4()}", principal=org, model="m1",
                      total_tokens=50, source="provider", owner_id=None, api_key_id=None,
                      ts=_dt(2026, 4, 4), created_at=_dt(2026, 4, 4)),
            ])
            await s.commit()

        start, end = _dt(2026, 4, 1), _dt(2026, 5, 1)
        async with sessionmaker() as s:
            by_user = await get_usage_by_user(s, org, start, end)
        async with sessionmaker() as s:
            by_key = await get_usage_by_key(s, org, start, end)
        async with sessionmaker() as s:
            summary = await get_usage_summary(s, org, start, end)

        # by_user: u1 (2 rows, 300 tokens) + null bucket (1 row, 50 tokens)
        users = {u.owner_id: u for u in by_user.users}
        assert set(users) == {"u1", None}
        assert users["u1"].requests == 2 and users["u1"].total_tokens == 300
        assert users[None].total_tokens == 50
        # by_key: k1, k2, null — all distinct
        keys = {k.api_key_id for k in by_key.api_keys}
        assert keys == {"k1", "k2", None}
        # both breakdowns reconcile with the summary total
        assert sum(u.total_tokens for u in by_user.users) == summary.total_tokens
        assert sum(k.total_tokens for k in by_key.api_keys) == summary.total_tokens
        assert sum(u.requests for u in by_user.users) == summary.requests
    finally:
        async with sessionmaker() as s:
            from sqlalchemy import delete

            await s.execute(delete(Usage).where(Usage.principal == org))
            await s.commit()
        await engine.dispose()
