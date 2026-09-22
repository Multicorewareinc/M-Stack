"""Application errors and their HTTP mapping. Routers raise these; the handlers
render the OpenAI-compatible error body so this gateway stays OpenAI-compatible:

    {"error": {"message": ..., "type": ..., "code": null, "param": null}}
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class AppError(Exception):
    status_code = 500
    error_type = "internal_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def body(self) -> dict:
        return {"error": {"message": self.message, "type": self.error_type, "code": None, "param": None}}


class BadRequestError(AppError):
    status_code = 400
    error_type = "invalid_request_error"


class UnauthorizedError(AppError):
    status_code = 401
    error_type = "authentication_error"


class BadGatewayError(AppError):
    status_code = 502
    error_type = "upstream_error"


class ServiceUnavailableError(AppError):
    status_code = 503
    error_type = "upstream_unavailable"


class PolicyDeniedError(AppError):
    """A pre-request policy denied the request. Status/type are policy-driven
    (see docs/policy-plane.md); defaults suit a rate limiter."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 429,
        error_type: str = "rate_limit_exceeded",
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.retry_after = retry_after


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle(_: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.error("app_error", extra={"status": exc.status_code, "type": exc.error_type})
        headers = {}
        retry_after = getattr(exc, "retry_after", None)
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        return JSONResponse(status_code=exc.status_code, content=exc.body(), headers=headers)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_error")
        return JSONResponse(
            status_code=500,
            content={"error": {"message": "Internal server error", "type": "internal_error", "code": None, "param": None}},
        )
