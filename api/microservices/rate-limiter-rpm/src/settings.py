"""Service configuration. Single Settings class, read from env (and optional .env).

RPM_RATE_LIMITS is one JSON object string (see .env.example). Absent/malformed => every
scope unlimited. A limit of 0 or absent means "no limit for that scope" (skipped).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class RateLimits:
    """Parsed RPM_RATE_LIMITS. 0/absent => unlimited for that scope."""

    user_default: int = 0
    model_default: int = 0
    user_overrides: dict[str, int] = field(default_factory=dict)
    model_overrides: dict[str, int] = field(default_factory=dict)
    user_model_overrides: dict[str, int] = field(default_factory=dict)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    log_level: str = "INFO"

    # Shared Valkey holding the fixed-window counters (prefix rl:rpm:).
    valkey_url: str = "redis://localhost:6379/0"

    # RPM limits as a JSON object. Empty => everything unlimited.
    # {"user_default":N,"model_default":N,"user_overrides":{...},
    #  "model_overrides":{...},"user_model_overrides":{"<principal>|<model>":N}}
    rpm_rate_limits: str = "{}"

    # Per-org plan limits (AD-04). Empty ADMIN_CP_URL => inert: falls back to RPM_RATE_LIMITS'
    # user_default/user_overrides for the "user" (org) scope, same as before this existed.
    admin_cp_url: str = ""  # e.g. "http://admin-control-plane:8000"
    service_api_key: str = ""  # bearer for admin-control-plane's /internal/v1/*
    plan_cache_ttl: int = 30  # seconds a resolved org plan limit is cached

    # Async counting via the event backbone (ADR-013, supersedes ADR-010 for RPM), reached
    # through the multistack.queue SDK seam (ADR-033). /check only READS the counters; a
    # consumer INCREMENTS them from the gateway's `request` events. Empty EVENT_BACKBONE_URL =>
    # no consumer runs => counters never advance => /check fails open (allows). See README.
    # QUEUE_BACKEND selects the backend the seam maps to (AD-04).
    queue_backend: str = "nats"  # env QUEUE_BACKEND; SDK maps this to a driver
    event_backbone_url: str = ""  # e.g. "nats://nats:4222" (the seam's url); empty = no consumer
    event_stream_name: str = "GATEWAY_EVENTS"
    event_stream_subject: str = "gateway.events"
    event_durable_name: str = "rpm-counter"  # durable JetStream consumer name
    dedupe_ttl: int = 120  # seconds to remember a request_id (>= window)

    def limits(self) -> RateLimits:
        try:
            raw = json.loads(self.rpm_rate_limits) or {}
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
