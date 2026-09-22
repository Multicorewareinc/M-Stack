"""Usage read aggregates (add-billing-usage-summary-api) — leaf module mirroring
`subscriptions.py`'s shape: query/business logic here, thin routes in `router.py`.

Filtering and bucketing both key off `COALESCE(ts, created_at)` (design D1) — `ts` is the source
event's own timestamp and can be NULL (`consumer.py::_persist`), while `created_at` is always
set. The SQL side does only the ANSI-portable filter (`func.coalesce`, identical on aiosqlite and
Postgres); day-bucketing and all summation happen in Python after the fetch (design D1/D3).

Callers MUST pass `start`/`end` as UTC-anchored `datetime`s, never a bare `date` (design D4a) —
that conversion happens once, at the router boundary, in `router.py`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from models import Usage
from schemas import (
    UsageBucketOut,
    UsageByKeyOut,
    UsageByKeyRow,
    UsageByUserOut,
    UsageByUserRow,
    UsagePeriod,
    UsageSummaryOut,
    UsageTimeseriesOut,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

# One row per matching usage record: (coalesced_ts, total_tokens, prompt_tokens, completion_tokens).
_PeriodRow = tuple[datetime, int | None, int | None, int | None]
# One row per matching usage record, grouped by a chosen column:
# (group_key, total_tokens, prompt_tokens, completion_tokens). group_key may be None.
_GroupedRow = tuple[str | None, int | None, int | None, int | None]


async def _fetch_period_rows(
    session: AsyncSession, org_id: str, start: datetime, end: datetime
) -> list[_PeriodRow]:
    coalesced = func.coalesce(Usage.ts, Usage.created_at)
    stmt = select(
        coalesced, Usage.total_tokens, Usage.prompt_tokens, Usage.completion_tokens
    ).where(Usage.principal == org_id, coalesced >= start, coalesced < end)
    result = await session.execute(stmt)
    return [tuple(row) for row in result.all()]


async def get_usage_summary(
    session: AsyncSession, org_id: str, start: datetime, end: datetime
) -> UsageSummaryOut:
    rows = await _fetch_period_rows(session, org_id, start, end)
    # D6: every matching row counts as a request; token sums are over the nullable columns only
    # (None contributes 0, never turns the running total into None).
    total_tokens = sum(r[1] for r in rows if r[1] is not None)
    prompt_tokens = sum(r[2] for r in rows if r[2] is not None)
    completion_tokens = sum(r[3] for r in rows if r[3] is not None)
    return UsageSummaryOut(
        requests=len(rows),
        total_tokens=total_tokens,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        period=UsagePeriod(start=start.date(), end=end.date()),
    )


async def get_usage_timeseries(
    session: AsyncSession, org_id: str, start: datetime, end: datetime
) -> UsageTimeseriesOut:
    rows = await _fetch_period_rows(session, org_id, start, end)

    # D5: build the zero-filled shape first — every UTC calendar day in [start, end) gets a
    # bucket, even with no usage — then add fetched rows on top. No day is ever omitted.
    buckets: dict[object, UsageBucketOut] = {}
    day = start.date()
    end_date = end.date()
    while day < end_date:
        buckets[day] = UsageBucketOut(
            date=day, requests=0, total_tokens=0, prompt_tokens=0, completion_tokens=0
        )
        day += timedelta(days=1)

    for coalesced_ts, total_tokens, prompt_tokens, completion_tokens in rows:
        bucket = buckets[coalesced_ts.date()]
        bucket.requests += 1
        bucket.total_tokens += total_tokens or 0
        bucket.prompt_tokens += prompt_tokens or 0
        bucket.completion_tokens += completion_tokens or 0

    return UsageTimeseriesOut(
        period=UsagePeriod(start=start.date(), end=end.date()),
        buckets=[buckets[d] for d in sorted(buckets)],
    )


async def _fetch_grouped_rows(
    session: AsyncSession,
    org_id: str,
    start: datetime,
    end: datetime,
    group_col: InstrumentedAttribute,
) -> list[_GroupedRow]:
    """Same org+period filter as `_fetch_period_rows` (design D1) but selecting a chosen
    grouping column (Usage.owner_id or Usage.api_key_id) in place of the coalesced ts —
    one shared fetch, so the COALESCE/date-bounds predicate lives in exactly one more place,
    not duplicated per breakdown."""
    coalesced = func.coalesce(Usage.ts, Usage.created_at)
    stmt = select(
        group_col, Usage.total_tokens, Usage.prompt_tokens, Usage.completion_tokens
    ).where(Usage.principal == org_id, coalesced >= start, coalesced < end)
    result = await session.execute(stmt)
    return [tuple(row) for row in result.all()]


def _group_counts(rows: list[_GroupedRow]) -> dict[str | None, dict[str, int]]:
    """Partition rows by their group key (a None key is a real bucket — the unattributed
    rows), summing the four count fields. None token values contribute 0 (never null the
    sum). Every row lands in exactly one bucket, so the buckets' sums reconcile with the
    org-wide summary by construction (design D2)."""
    groups: dict[str | None, dict[str, int]] = {}
    for key, total_tokens, prompt_tokens, completion_tokens in rows:
        acc = groups.get(key)
        if acc is None:
            acc = {"requests": 0, "total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0}
            groups[key] = acc
        acc["requests"] += 1
        acc["total_tokens"] += total_tokens or 0
        acc["prompt_tokens"] += prompt_tokens or 0
        acc["completion_tokens"] += completion_tokens or 0
    return groups


def _sorted_group_keys(groups: dict[str | None, dict[str, int]]) -> list[str | None]:
    """Deterministic order: real ids sorted, the None (unattributed) bucket last."""
    real = sorted(k for k in groups if k is not None)
    return real + ([None] if None in groups else [])


async def get_usage_by_user(
    session: AsyncSession, org_id: str, start: datetime, end: datetime
) -> UsageByUserOut:
    groups = _group_counts(await _fetch_grouped_rows(session, org_id, start, end, Usage.owner_id))
    users = [
        UsageByUserRow(owner_id=key, **groups[key]) for key in _sorted_group_keys(groups)
    ]
    return UsageByUserOut(period=UsagePeriod(start=start.date(), end=end.date()), users=users)


async def get_usage_by_key(
    session: AsyncSession, org_id: str, start: datetime, end: datetime
) -> UsageByKeyOut:
    groups = _group_counts(await _fetch_grouped_rows(session, org_id, start, end, Usage.api_key_id))
    api_keys = [
        UsageByKeyRow(api_key_id=key, **groups[key]) for key in _sorted_group_keys(groups)
    ]
    return UsageByKeyOut(period=UsagePeriod(start=start.date(), end=end.date()), api_keys=api_keys)
