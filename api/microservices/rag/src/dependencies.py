"""Request dependencies: the bearer check every /v1 route sits behind."""

from __future__ import annotations

import secrets

from fastapi import Request

from errors import UnauthorizedError


class ServiceKeyUnsetError(RuntimeError):
    """SERVICE_API_KEY has no configured value: a deployment
    misconfiguration, never a silent bypass. Surfaces as a 500."""


def require_service_key(request: Request) -> None:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise UnauthorizedError("Missing or malformed Authorization header")
    key = request.app.state.settings.service_api_key
    if not key:
        raise ServiceKeyUnsetError("SERVICE_API_KEY is not configured.")
    if not secrets.compare_digest(token, key):
        raise UnauthorizedError("Invalid service API key")
