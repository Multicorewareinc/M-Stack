"""Request dependencies.

- `require_service_key` gates every /v1 route (and /internal/v1/users, /internal/v1/organization)
  on either the BROAD fixed service bearer (secrets.compare_digest, constant-time — mirrors
  admin-control-plane's `require_admin_key`) OR a real org member's verified JWT (modules.auth)
  issued by POST /api/auth/login. The service bearer is the trusted BFF/portal-backend credential.
- `require_mg_service_key` gates ONLY the model-gateway's own call site
  (`GET /internal/v1/api-keys/verify`) on a SEPARATE, narrower bearer. The model-gateway is
  internet-adjacent (it terminates untrusted per-org API-key traffic) and must not hold a
  credential that also opens the broader /v1 users/roles/rbac/organizations/permissions surface —
  a shared bearer there would let any model-gateway compromise read/mutate any tenant's users,
  roles, and permission grants via the same `X-Organization-Id`-trusted routes.
- `require_org_context` yields the request's organization_id — the tenant scope every org-owned
  query filters on (SP-03+).

The auth module has landed, closing the upgrade path this file used to describe as future work
(AD-06): a request authenticated via the trusted service bearer still has its org id read from
the `X-Organization-Id` header (unchanged — the caller is an authenticated service, not an
end-user); a request authenticated via a real JWT instead has its org id read from the verified
token's own `org` claim, never a client-supplied header — a signed-in user cannot claim to be
scoped to an organization other than the one their token says they belong to.
"""

from __future__ import annotations

import secrets
import uuid

import httpx
from fastapi import Request

from errors import UnauthorizedError, UnprocessableError
from modules.auth.jwt import decode_access_token

ORG_HEADER = "X-Organization-Id"


class ServiceKeyUnsetError(RuntimeError):
    """A gating bearer (SERVICE_API_KEY / MG_SERVICE_API_KEY) has no configured value — a
    deployment misconfiguration, never a silent bypass. Surfaces as a 500 via the app's catch-all
    handler."""


def _require_key(configured: str | None, *, name: str) -> str:
    if not configured:
        raise ServiceKeyUnsetError(f"{name} is not configured.")
    return configured


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def require_service_key(request: Request) -> None:
    token = _bearer_token(request)
    if token is None:
        raise UnauthorizedError("Missing or malformed Authorization header")
    settings = request.app.state.settings
    key = _require_key(settings.service_api_key, name="SERVICE_API_KEY")
    if secrets.compare_digest(token, key):
        return
    # Unlike modules.auth's own current_user() (where an unset secret is a loud 500 — that IS the
    # auth flow), here JWT support is opportunistic on top of the static key: a deployment that
    # hasn't configured JWT_SECRET simply can't authenticate via JWT, same as before this existed.
    try:
        decode_access_token(token, secret=settings.jwt_secret)
    except Exception:
        raise UnauthorizedError("Invalid service API key") from None


def require_mg_service_key(request: Request) -> None:
    """Gate the model-gateway's sole call site with a bearer distinct from `service_api_key` —
    least-privilege: this credential opens nothing but GET /internal/v1/api-keys/verify."""
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise UnauthorizedError("Missing or malformed Authorization header")
    key = _require_key(request.app.state.settings.mg_service_api_key, name="MG_SERVICE_API_KEY")
    if not secrets.compare_digest(token, key):
        raise UnauthorizedError("Invalid service API key")


def require_org_context(request: Request) -> uuid.UUID:
    """The tenant scope for org-owned operations. 422 when absent/malformed for a service-key
    caller; 401 when a JWT caller's token carries no usable `org` claim.

    Only touches request.app.state (unavailable to a route-less unit test constructing a bare
    Request) when a bearer token is actually present — a request with none falls straight through
    to the header-based path below, same as before this dependency knew about JWTs. In practice
    that "no token" case never reaches here anyway: require_service_key runs first on every route
    that also uses this dependency and would already have rejected it with 401.
    """
    token = _bearer_token(request)
    if token is not None:
        settings = request.app.state.settings
        service_key = _require_key(settings.service_api_key, name="SERVICE_API_KEY")
        is_service_key = secrets.compare_digest(token, service_key)
        if not is_service_key:
            try:
                claims = decode_access_token(token, secret=settings.jwt_secret)
            except Exception:
                raise UnauthorizedError("The access token is invalid") from None
            org_claim = claims.get("org")
            if not org_claim:
                raise UnauthorizedError("The access token has no organization")
            try:
                return uuid.UUID(str(org_claim))
            except ValueError as exc:
                raise UnauthorizedError("The access token organization is invalid") from exc

    raw = request.headers.get(ORG_HEADER, "")
    if not raw:
        raise UnprocessableError(f"Missing {ORG_HEADER} header")
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise UnprocessableError(f"Malformed {ORG_HEADER} header") from exc


def get_admin_client(request: Request) -> httpx.AsyncClient:
    """The outbound client to Admin CP's internal API (permission validation, ADR-018)."""
    return request.app.state.admin_client


def get_billing_client(request: Request) -> httpx.AsyncClient:
    """The outbound client to billing's internal API (usage proxy, add-org-cp-usage-proxy)."""
    return request.app.state.billing_client
