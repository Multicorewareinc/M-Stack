"""HTTP routes. Ops-only (GET /health + GET /metrics, mirroring the enricher's precedent — no
/ready; the consumer-active gauge on /metrics is the readiness signal) plus, since
add-billing-plan-subscription-linkage, the internal service-to-service subscription-linkage
endpoint (bearer-gated) and, since add-billing-stripe-webhooks, the inbound Stripe webhook
endpoint (signature-gated, not bearer-gated — Stripe is the caller)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time

import usage
from consumer import CONSUMER_ACTIVE
from dependencies import require_service_key
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from schemas import (
    SubscriptionLinkRequest,
    SubscriptionLinkResponse,
    UsageByKeyOut,
    UsageByUserOut,
    UsageSummaryOut,
    UsageTimeseriesOut,
)
from subscriptions import resolve_and_link_subscription
from webhooks import process_webhook

from errors import UnprocessableError

router = APIRouter()


def _now_utc() -> datetime:
    """Isolated so tests can `monkeypatch.setattr(router, "_now_utc", ...)` to control
    `_default_period()`'s "current month" without a real-time dependency (add-billing-usage-
    summary-api, council review fix — avoids flakiness near a UTC month boundary)."""
    return datetime.now(UTC)


def _default_period() -> tuple[datetime, datetime]:
    """The current UTC calendar month as `[first_of_month, first_of_next_month)` — used by both
    usage routes when either `start` or `end` is omitted (design D4: never a partial default)."""
    now = _now_utc()
    start = datetime(now.year, now.month, 1, tzinfo=UTC)
    end = datetime(now.year + 1, 1, 1, tzinfo=UTC) if now.month == 12 else datetime(
        now.year, now.month + 1, 1, tzinfo=UTC
    )
    return start, end


def _to_utc_bounds(d: date) -> datetime:
    """Convert a caller-supplied `date` query param to a UTC-midnight-anchored `datetime`
    (design D4a) — `usage.py`'s functions must never receive a bare `date`, since comparing one
    directly against a tz-aware `DateTime` column is not portably defined across asyncpg and
    aiosqlite."""
    return datetime.combine(d, time.min, tzinfo=UTC)


def _resolve_period(start: date | None, end: date | None) -> tuple[datetime, datetime]:
    """Shared by both usage routes: `start`/`end` are provided together or not at all — a
    caller-supplied pair is converted via `_to_utc_bounds`; an omitted pair defaults together via
    `_default_period()` (design D4). Rejects an inverted/empty range."""
    if start is None or end is None:
        return _default_period()
    start_dt, end_dt = _to_utc_bounds(start), _to_utc_bounds(end)
    if end_dt <= start_dt:
        raise UnprocessableError("end must be after start", field="end")
    return start_dt, end_dt


def _sync_consumer_gauge(consumer) -> None:
    CONSUMER_ACTIVE.set(1.0 if (consumer is not None and consumer.active) else 0.0)


@router.get("/health", tags=["ops"])
async def health() -> dict:
    return {"status": "ok"}


@router.get("/metrics", tags=["ops"])
async def metrics(request: Request) -> Response:
    _sync_consumer_gauge(request.app.state.consumer)
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/health/stripe-cli", tags=["ops"])
async def health_stripe_cli(request: Request) -> dict:
    manager = request.app.state.stripe_cli_manager
    if manager is None:
        return {"enabled": False, "running": False, "pid": None}
    return {"enabled": True, "running": manager.is_running(), "pid": manager.get_pid()}


@router.post("/webhooks/stripe", tags=["webhooks"])
async def stripe_webhook(request: Request) -> Response:
    webhook_secret = request.app.state.settings.stripe_webhook_secret
    # Config-level inert gate (ADR-006 rule 3), checked FIRST — no body read, no signature
    # check, no DB session when unset. Never accept an unverified webhook.
    if not webhook_secret:
        return Response(status_code=501)

    body = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker() as session:
        status_code, response_body = await process_webhook(
            body, sig_header, session, webhook_secret=webhook_secret
        )
    return Response(
        status_code=status_code, content=json.dumps(response_body), media_type="application/json"
    )


# Gated by require_service_key (ADR-018 convention) — billing's first bearer-gated HTTP write
# surface. admin-control-plane calls this best-effort on org creation/plan change (ADR-032).
internal_router = APIRouter(
    prefix="/internal/v1", tags=["internal"], dependencies=[Depends(require_service_key)]
)


@internal_router.post("/subscriptions", response_model=SubscriptionLinkResponse)
async def link_subscription(
    body: SubscriptionLinkRequest, request: Request
) -> SubscriptionLinkResponse:
    stripe_client = request.app.state.stripe_client
    # Config-level inert gate (ADR-006 rule 3), distinct from the price-level inert gate inside
    # resolve_and_link_subscription — no Stripe client configured at all means no DB session is
    # opened either (NOT a `Depends(get_session)` route parameter, which would always open one
    # regardless of this branch).
    if stripe_client is None:
        return SubscriptionLinkResponse(outcome="inert", stripe_subscription_id=None)

    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker() as session:
        result = await resolve_and_link_subscription(
            stripe_client, session, principal=body.org_id, stripe_price_id=body.stripe_price_id,
        )
        await session.commit()
    return SubscriptionLinkResponse(**result)


@internal_router.get("/usage/summary", response_model=UsageSummaryOut)
async def usage_summary(
    request: Request,
    org_id: str = Query(..., min_length=1),
    start: date | None = None,
    end: date | None = None,
) -> UsageSummaryOut:
    start_dt, end_dt = _resolve_period(start, end)
    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker() as session:
        return await usage.get_usage_summary(session, org_id, start_dt, end_dt)


@internal_router.get("/usage/timeseries", response_model=UsageTimeseriesOut)
async def usage_timeseries(
    request: Request,
    org_id: str = Query(..., min_length=1),
    start: date | None = None,
    end: date | None = None,
) -> UsageTimeseriesOut:
    start_dt, end_dt = _resolve_period(start, end)
    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker() as session:
        return await usage.get_usage_timeseries(session, org_id, start_dt, end_dt)


@internal_router.get("/usage/by-user", response_model=UsageByUserOut)
async def usage_by_user(
    request: Request,
    org_id: str = Query(..., min_length=1),
    start: date | None = None,
    end: date | None = None,
) -> UsageByUserOut:
    start_dt, end_dt = _resolve_period(start, end)
    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker() as session:
        return await usage.get_usage_by_user(session, org_id, start_dt, end_dt)


@internal_router.get("/usage/by-key", response_model=UsageByKeyOut)
async def usage_by_key(
    request: Request,
    org_id: str = Query(..., min_length=1),
    start: date | None = None,
    end: date | None = None,
) -> UsageByKeyOut:
    start_dt, end_dt = _resolve_period(start, end)
    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker() as session:
        return await usage.get_usage_by_key(session, org_id, start_dt, end_dt)
