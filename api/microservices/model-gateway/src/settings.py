"""Service configuration. Single Settings class, read from env (and optional .env)."""

from __future__ import annotations

import json

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Auth (SP-02, ADR-026): /v1 bearer tokens are per-org API keys verified against Org CP through a
    # Redis read-through cache. The static single-key auth is removed.
    valkey_url: str = "redis://localhost:6379/0"
    org_cp_internal_url: str = "http://organization-control-plane:8000"
    # Bearer for Org CP's GET /internal/v1/api-keys/verify — ONLY that route. Deliberately distinct
    # from Org CP's broader `service_api_key` (which opens /v1 users/roles/rbac/organizations): the
    # model-gateway is internet-adjacent (it terminates untrusted per-org API-key traffic), so a
    # gateway compromise must not also yield tenant-wide read/write access on Org CP's /v1 surface
    # (council review, least-privilege fix). Must be provisioned as Org CP's `mg_service_api_key`.
    org_verify_api_key: str = "change-me"
    apikey_cache_ttl_seconds: int = 300  # verify-cache TTL ceiling (capped at the key's remaining life)

    log_level: str = "INFO"

    # Upstream inference server the gateway proxies to (OpenAI-compatible:
    # vLLM, TGI, Ollama, OpenAI, or any OpenAI-compatible endpoint).
    upstream_url: str = "http://localhost:8000"
    upstream_api_key: str | None = None

    # Optional per-model routing as JSON: {"model-name": "http://other-upstream"}.
    # Unlisted models fall back to upstream_url.
    model_routes: str = "{}"

    request_timeout: float = 60.0

    # Pre-request policy chain (see docs/policy-plane.md). Empty => inert.
    # Comma-separated URLs of policy services implementing POST /check.
    policy_endpoints: str = ""
    policy_timeout_ms: int = 50
    policy_fail_mode: str = "closed"  # "closed" (block on policy error) | "open" (allow)

    # Event backbone (ADR-007/008/009). Empty EVENT_BACKBONE_URL => inert:
    # no publisher built, request/response path byte-identical to no feature.
    event_backbone_url: str = ""            # e.g. "nats://nats:4222"
    event_stream_name: str = "GATEWAY_EVENTS"
    event_stream_subject: str = "gateway.events"
    event_capture_bodies: bool = False       # off => events carry no prompt/response text
    event_max_inflight: int = 1000           # bounded background publishes; drop over cap

    def routes(self) -> dict[str, str]:
        return json.loads(self.model_routes)

    def policy_endpoint_list(self) -> list[str]:
        return [e.strip() for e in self.policy_endpoints.split(",") if e.strip()]
