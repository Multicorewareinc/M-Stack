"""Request dependencies.

`require_service_key` gates the `/internal/v1/*` service-to-service surface (admin-control-plane
calling in for the subscription-linkage endpoint, add-billing-plan-subscription-linkage) on
`service_api_key` — mirrors admin-control-plane's/organization-control-plane's
`require_service_key` exactly, including the same "unset key fails closed as 500" convention.
"""

from __future__ import annotations

import secrets

from fastapi import Request

from errors import UnauthorizedError


class ServiceKeyUnsetError(RuntimeError):
    """SERVICE_API_KEY has no configured value — a deployment misconfiguration, never a silent
    bypass. Surfaces as a 500 via the app's catch-all handler."""


def require_service_key(request: Request) -> None:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise UnauthorizedError("Missing or malformed Authorization header")
    key = request.app.state.settings.service_api_key
    if not key:
        raise ServiceKeyUnsetError("SERVICE_API_KEY is not configured.")
    if not secrets.compare_digest(token, key):
        raise UnauthorizedError("Invalid service API key")
