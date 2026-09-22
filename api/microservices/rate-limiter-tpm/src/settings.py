"""Service configuration. Single Settings class, read from env (and optional .env).

RATE_LIMITS is one JSON object string (see .env.example). Absent/malformed => every
scope unlimited. A limit of 0 or absent means "no limit for that scope" (skipped).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class RateLimits:
    """Parsed RATE_LIMITS. 0/absent => unlimited for that scope."""

    user_default: int = 0
    model_default: int = 0
    user_overrides: dict[str, int] = field(default_factory=dict)
    model_overrides: dict[str, int] = field(default_factory=dict)
    user_model_overrides: dict[str, int] = field(default_factory=dict)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    log_level: str = "INFO"

    # Shared Valkey holding the fixed-window counters (prefix rl:tpm:), SAME instance as
    # rate-limiter-rpm (ADR-011).
    valkey_url: str = "redis://localhost:6379/0"

    # TPM limits as a JSON object (same shape as RPM's RATE_LIMITS). Empty => everything
    # unlimited. Values are token counts, not request counts.
    rate_limits: str = "{}"

    # Per-org plan limits (AD-04). Empty ADMIN_CP_URL => inert: falls back to RATE_LIMITS'
    # user_default/user_overrides for the "user" (org) scope, same as before this existed.
    admin_cp_url: str = ""                  # e.g. "http://admin-control-plane:8000"
    service_api_key: str = ""               # bearer for admin-control-plane's /internal/v1/*
    plan_cache_ttl: int = 30                # seconds a resolved org plan limit is cached

    # Async counting via the event backbone (ADR-007/008/030, AD-01). /check only READS
    # the counters; a NATS JetStream consumer INCREMENTs them from the gateway's `response`
    # events — via the ENRICHED stream (ADR-030): the `enricher` service guarantees a
    # normalized `usage.total_tokens` on every response event, so this service never calls
    # the tokenizer itself (that fallback moved to `enricher`, ADR-030 AD-02). Empty
    # EVENT_BACKBONE_URL => no consumer runs => counters never advance => /check fails open.
    event_backbone_url: str = ""                        # e.g. "nats://nats:4222"; empty = no consumer
    event_stream_name: str = "GATEWAY_EVENTS_ENRICHED"
    event_stream_subject: str = "gateway.events.enriched"
    event_durable_name: str = "tpm-counter-enriched"     # durable JetStream consumer name
    dedupe_ttl: int = 120                                # seconds to remember a request_id (>= window)

    def limits(self) -> RateLimits:
        try:
            raw = json.loads(self.rate_limits) or {}
        except ValueError:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        return RateLimits(
            user_default=int(raw.get("user_default") or 0),
            model_default=int(raw.get("model_default") or 0),
            user_overrides=dict(raw.get("user_overrides") or {}),
            model_overrides=dict(raw.get("model_overrides") or {}),
            user_model_overrides=dict(raw.get("user_model_overrides") or {}),
        )
