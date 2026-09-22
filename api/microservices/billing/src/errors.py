"""Application errors and their handlers. Reuses the model-gateway/rate-limiter/admin-control-
plane JSON envelope `{"error": {"message": ..., "type": ...}}` (ADR-003). Minimal this
increment — the new internal endpoint (add-billing-plan-subscription-linkage) has no 404/409
cases; a malformed body is Pydantic's own 422, and unauthorized is the only typed error
needed."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class AppError(Exception):
    status_code = 500
    error_type = "internal_error"
    default_message = "Internal server error"

    def __init__(self, message: str | None = None, *, field: str | None = None) -> None:
        message = message if message is not None else self.default_message
        super().__init__(message)
        self.message = message
        self.field = field

    def body(self) -> dict:
        payload: dict = {"message": self.message, "type": self.error_type}
        if self.field is not None:
            payload["field"] = self.field
        return {"error": payload}


class UnauthorizedError(AppError):
    status_code = 401
    error_type = "unauthorized"


class UnprocessableError(AppError):
    # billing's first 422 case (add-billing-usage-summary-api) — mirrors organization-control-
    # plane's UnprocessableError exactly. Used for a request-shape problem type validation alone
    # can't catch (e.g. an inverted usage period), never for a missing/malformed required field
    # (Pydantic/FastAPI already 422s those on their own).
    status_code = 422
    error_type = "unprocessable"


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
