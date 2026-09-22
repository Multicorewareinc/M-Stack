"""Request dependencies.

- `require_admin_key` gates every human-facing /v1 route on either the fixed super-admin bearer
  (secrets.compare_digest, constant-time — mirrors model-gateway's `require_api_key`) OR a real
  super-admin's verified JWT (modules.auth) issued by POST /api/auth/login. The static key serves
  service-to-service/dev callers; the JWT serves the SPA acting as a signed-in super-admin. This
  closes the "OIDC/JWT validation" upgrade path this dependency used to describe as future work —
  the dependency seam is unchanged, only the token check grew a second branch.
- `require_service_key` gates the `/internal/v1/*` service-to-service surface (Org CP calling in for
  permission-catalog/plan reads) on `service_api_key` — a bearer DISTINCT from `admin_api_key`.
  Previously the internal routers reused `require_admin_key`, which forced every deployment to set
  `ADMIN_API_KEY == SERVICE_API_KEY`, handing the super-admin credential to Org CP (and transitively
  to any service Org CP itself trusts). Splitting the two seams means a leaked/compromised service
  key no longer doubles as super-admin access to this control plane's own /v1 routes.
"""

from __future__ import annotations

import secrets

import httpx
from fastapi import Request
from sqlalchemy.ext.asyncio import async_sessionmaker

from errors import UnauthorizedError
from modules.auth.jwt import decode_access_token


class ServiceKeyUnsetError(RuntimeError):
    """A gating bearer (ADMIN_API_KEY / SERVICE_API_KEY) has no configured value — a deployment
    misconfiguration, never a silent bypass. Surfaces as a 500 via the app's catch-all handler."""


def _require_key(configured: str | None, *, name: str) -> str:
    if not configured:
        raise ServiceKeyUnsetError(f"{name} is not configured.")
    return configured


def require_admin_key(request: Request) -> None:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise UnauthorizedError("Missing or malformed Authorization header")
    settings = request.app.state.settings
    key = _require_key(settings.admin_api_key, name="ADMIN_API_KEY")
    if secrets.compare_digest(token, key):
        return
    # Unlike modules.auth's own current_user() (where an unset secret is a loud 500 — that IS the
    # auth flow), here JWT support is opportunistic on top of the static key: a deployment that
    # hasn't configured JWT_SECRET simply can't authenticate via JWT, same as before this existed.
    try:
        decode_access_token(token, secret=settings.jwt_secret)
    except Exception:
        raise UnauthorizedError("Invalid admin API key") from None


def require_service_key(request: Request) -> None:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise UnauthorizedError("Missing or malformed Authorization header")
    key = _require_key(request.app.state.settings.service_api_key, name="SERVICE_API_KEY")
    if not secrets.compare_digest(token, key):
        raise UnauthorizedError("Invalid service API key")


def get_org_client(request: Request) -> httpx.AsyncClient:
    """Outbound client to Org CP's internal API (org-creation workflow, ADR-018/AD-03)."""
    return request.app.state.org_client


def get_billing_client(request: Request) -> httpx.AsyncClient:
    """Outbound client to billing's internal API (best-effort subscription notification,
    ADR-032/add-billing-plan-subscription-linkage)."""
    return request.app.state.billing_client


def get_sessionmaker(request: Request) -> async_sessionmaker:
    """The app's async_sessionmaker — used by the org-creation workflow to run its two
    transactions directly (it cannot use the single-commit get_session dependency, D1)."""
    return request.app.state.sessionmaker
