"""Application errors and their HTTP mapping. Routes/dependencies/service
raise these; the handlers render a consistent JSON body:

    {"error": {"message": ..., "type": ...}}
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
        return {"error": {"message": self.message, "type": self.error_type}}


class UnauthorizedError(AppError):
    status_code = 401
    error_type = "unauthorized"


class ConfigurationError(AppError):
    """The server itself is misconfigured (e.g. SERVICE_API_KEY unset) --
    distinct from UnauthorizedError, which means the *caller* got auth
    wrong. Fails closed at 500, never silently treated as "no auth"."""

    status_code = 500
    error_type = "configuration_error"


class NotFoundError(AppError):
    status_code = 404
    error_type = "not_found"


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle(_: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.error("app_error", extra={"status": exc.status_code, "type": exc.error_type})
        return JSONResponse(status_code=exc.status_code, content=exc.body())

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_error")
        return JSONResponse(
            status_code=500,
            content={"error": {"message": "Internal server error", "type": "internal_error"}},
        )
