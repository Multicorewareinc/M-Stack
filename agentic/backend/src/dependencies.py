"""Auth / per-request guards.

Single shared API key, checked via the X-API-Key header. Attach with
`dependencies=[Depends(require_api_key)]` on every route except /health.
"""

from __future__ import annotations

from fastapi import Header, Request

from errors import ConfigurationError, UnauthorizedError
from settings import Settings


def require_api_key(request: Request, x_api_key: str | None = Header(default=None)) -> None:
    settings: Settings = request.app.state.settings
    if not settings.service_api_key:
        # Fail closed, not open: an unset key means auth is misconfigured,
        # not "auth is off." A real deployment must set this.
        raise ConfigurationError("SERVICE_API_KEY is not configured on the server")
    if not x_api_key or x_api_key != settings.service_api_key:
        raise UnauthorizedError("missing or invalid X-API-Key")
