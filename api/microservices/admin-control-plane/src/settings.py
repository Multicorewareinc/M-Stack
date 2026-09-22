"""Service configuration. Single Settings class, read from env (and optional .env).

Mirrors the pydantic-settings shape used across the repo's services (ADR-003).
"""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    log_level: str = "INFO"

    # Admin database. SQLAlchemy async URL (asyncpg driver in prod). Alembic reads the same
    # value from the environment. Offline tests never use this — they inject an aiosqlite
    # engine into create_app (AD-03/AD-04).
    database_url: str = "postgresql+asyncpg://admin:admin@localhost:5432/admin_control_plane"

    # Super-admin bearer token. Every /v1/* route is checked against this with
    # secrets.compare_digest (AD-05). No default (council review fix: a literal "change-me" default
    # is a known credential shippable by accident) — an unset key fails closed as a 500 the first
    # time require_admin_key runs, mirroring how jwt_secret is handled, never silently admitting
    # every bearer or falling back to a guessable value.
    admin_api_key: str | None = None

    # Seed the three default plan tiers on startup (insert-missing only, never overwrite).
    seed_plans: bool = True

    # Seed the default master permission catalog on startup (insert-missing by slug, ADR-019).
    seed_permissions: bool = True

    # Org CP internal API — Admin CP calls this to initialize an org's tenant record during the
    # org-creation workflow (ADR-018/AD-03); `service_api_key` is the outbound bearer Org CP checks.
    # It is ALSO the inbound bearer `require_service_key` checks on this service's own
    # `/internal/v1/*` routes (Org CP calling in for permission-catalog/plan reads) — a single
    # shared service-to-service secret between the two peer control planes, deliberately DISTINCT
    # from `admin_api_key` (the human/UI super-admin credential) so a compromised or shared service
    # key never doubles as super-admin access.
    org_cp_internal_url: str = "http://organization-control-plane:8000"
    # No default (council review fix, same rationale as admin_api_key above) — unset fails closed
    # as a 500 the first time require_service_key runs.
    service_api_key: str | None = None
    org_timeout_ms: int = 3000

    # Billing internal API — Admin CP best-effort-notifies billing of an org's resolved plan
    # price on org creation and plan change (add-billing-plan-subscription-linkage, ADR-032).
    # Reuses the SAME `service_api_key` outbound bearer as the Org CP client above (one shared
    # service-to-service secret across the platform's internal APIs, per the existing convention).
    billing_internal_url: str = "http://billing:8000"
    billing_timeout_ms: int = 3000

    # ── Local auth (modules/auth) config knobs (design D7) ────────────────────────────────────
    # HS256 signing secret. No default — an unset secret fails login closed as 500 (never an
    # unsigned token); read only inside modules/auth/jwt.py and never logged. Shared with Org CP.
    jwt_secret: SecretStr | None = None
    jwt_access_ttl_seconds: int = 3600
    jwt_refresh_ttl_seconds: int = 604800
    local_auth_enabled: bool = True
    password_min_length: int = 12
    argon2_memory_cost: int = 19456
    argon2_time_cost: int = 2
    argon2_parallelism: int = 1
    login_max_attempts: int = 5
    login_lockout_window_seconds: int = 900
    login_backoff_base_seconds: int = 30
    login_backoff_ceiling_seconds: int = 3600
    # Fail-CLOSED by default (council review fix): on a Valkey outage, check_locked re-raises (a
    # login attempt 500s) rather than silently admitting unlimited unthrottled credential guessing
    # for the outage's duration. Flip to True only for deployments that have explicitly decided
    # availability of local-auth login outweighs brute-force exposure during a Valkey blip.
    login_lockout_fail_open: bool = False
    trusted_proxy_count: int = 0
    # Redis for the brute-force lockout counters (redis.asyncio client from this URL).
    valkey_url: str = "redis://localhost:6379/0"
