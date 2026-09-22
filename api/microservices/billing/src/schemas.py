"""Pydantic v2 DTOs for the internal HTTP surface (add-billing-plan-subscription-linkage,
add-billing-usage-summary-api)."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class SubscriptionLinkRequest(BaseModel):
    org_id: str
    stripe_price_id: str | None = None


class SubscriptionLinkResponse(BaseModel):
    outcome: str
    stripe_subscription_id: str | None


class UsagePeriod(BaseModel):
    start: date
    end: date


class UsageSummaryOut(BaseModel):
    requests: int
    total_tokens: int
    prompt_tokens: int
    completion_tokens: int
    period: UsagePeriod


class UsageBucketOut(BaseModel):
    date: date
    requests: int
    total_tokens: int
    prompt_tokens: int
    completion_tokens: int


class UsageTimeseriesOut(BaseModel):
    period: UsagePeriod
    buckets: list[UsageBucketOut]


class UsageByUserRow(BaseModel):
    # None => the unattributed bucket (pre-attribution/historical rows), never a string sentinel.
    owner_id: str | None
    requests: int
    total_tokens: int
    prompt_tokens: int
    completion_tokens: int


class UsageByUserOut(BaseModel):
    period: UsagePeriod
    users: list[UsageByUserRow]


class UsageByKeyRow(BaseModel):
    # None => the unattributed bucket (pre-attribution/historical rows), never a string sentinel.
    api_key_id: str | None
    requests: int
    total_tokens: int
    prompt_tokens: int
    completion_tokens: int


class UsageByKeyOut(BaseModel):
    period: UsagePeriod
    api_keys: list[UsageByKeyRow]
