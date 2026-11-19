"""Service configuration. Single Settings class, read from env (and optional .env).

Mirrors the pydantic-settings shape used across the repo's services (ADR-003).
"""

from __future__ import annotations

from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    log_level: str = "INFO"

    # Org database. SQLAlchemy async URL (asyncpg driver in prod). Alembic reads the same value
    # from the environment. Offline tests never use this — they inject an aiosqlite engine into
    # create_app (ADR-015/ADR-020).
    database_url_org: str = "postgresql+asyncpg://org:org@localhost:5432/org_control_plane"

    # Service bearer token. Every /v1/* and /internal/v1/users, /internal/v1/organization route is
    # checked against this with secrets.compare_digest (AD-06) — the broad, trusted BFF/portal-backend
    # credential. Also sent as the outbound bearer to Admin CP's internal API (shared service key for
    # now). No default (council review fix: a literal "change-me" default is a known credential
    # shippable by accident) — an unset key fails closed as a 500 the first time require_service_key
    # runs, mirroring how jwt_secret is handled.
    service_api_key: str | None = None

    # Narrower bearer, distinct from service_api_key, scoped to ONLY the model-gateway's own call
    # site: GET /internal/v1/api-keys/verify. The model-gateway is internet-adjacent (it terminates
    # untrusted per-org API-key traffic), so it must not hold a credential that also opens the
    # broader /v1 users/roles/rbac/organizations/permissions surface (council review finding:
    # tenant isolation on /v1 rests on a header trusted under whichever bearer presents it — the
    # fix is least-privilege on which callers get that bearer, not the header check itself). No
    # default, same rationale as service_api_key above; must differ from service_api_key.
    mg_service_api_key: str | None = None

    # Admin CP internal API base URL — Org CP validates role_permissions.permission_id against
    # the master permission list here (ADR-018/ADR-019). Bounded timeout on the call.
    admin_cp_internal_url: str = "http://admin-control-plane:8000"
    admin_timeout_ms: int = 3000

    # e2e/CI only: when set, a single fixed verify API key — plus its org and active owner — is seeded
    # on startup (src/seed.py) so the model-gateway's fixed test bearer resolves against a real row
    # without a human minting one. Empty (the default) disables seeding entirely; never set in prod.
    seed_api_key: str | None = None

    # Billing internal API base URL — Org CP proxies the org-scoped usage read endpoints here
    # (add-org-cp-usage-proxy), mirroring the Admin CP client above exactly. Bounded timeout on
    # the call.
    billing_internal_url: str = "http://billing:8000"
    billing_timeout_ms: int = 3000

    # ── Local auth (modules/auth) config knobs (design D6) ────────────────────────────────────
    # HS256 signing secret. No default — an unset secret fails login closed as 500 (never an
    # unsigned token); read only inside modules/auth/jwt.py and never logged.
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

    # ── Invitations (modules/invitations) ─────────────────────────────────────────────────────
    # How long an invitation link stays valid.
    invitation_ttl_hours: int = 168
    # Public base URL of the Organization Portal (e.g. https://org.example.com). When set, invitation
    # responses carry an absolute `invite_url`; otherwise only the relative `invite_path`.
    org_portal_base_url: str | None = None

    # ── Outgoing mail (SMTP), used to email invitations ───────────────────────────────────────
    # Needs smtp_host AND smtp_from_address, plus org_portal_base_url (the email carries an absolute
    # link). Anything missing = invitations are still created and the link is shown to copy by hand.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    # starttls: plain connect then upgrade (port 587). ssl: TLS from the first byte (port 465).
    # none: no encryption -- only for a local relay or a test mailbox.
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    smtp_from_address: str | None = None
    smtp_from_name: str = "Multistack"
    smtp_timeout_seconds: float = 10.0
    # Overall cap on one send (connect + TLS + login + message). smtp_timeout_seconds applies to each
    # network step, so without this a slow server could add up to several times that. Callers waiting on
    # the invitation (the Admin CP) allow slightly longer than this.
    smtp_send_deadline_seconds: float = 20.0
