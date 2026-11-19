"""Application errors and their handlers, in the JSON envelope the other
services use: `{"error": {"message": ..., "type": ...}}`."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class AppError(Exception):
    status_code = 500
    error_type = "internal_error"
    default_message = "Internal server error"

    def __init__(self, message: str | None = None) -> None:
        message = message if message is not None else self.default_message
        super().__init__(message)
        self.message = message

    def body(self) -> dict:
        return {"error": {"message": self.message, "type": self.error_type}}


class UnauthorizedError(AppError):
    status_code = 401
    error_type = "unauthorized"


class UnavailableError(AppError):
    """A dependency (embedding server, vector database, index) can't serve
    the request right now."""

    status_code = 503
    error_type = "unavailable"


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
