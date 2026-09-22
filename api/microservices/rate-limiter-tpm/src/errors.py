"""Application errors and their handlers. This service's happy path never errors
(/check always returns 200 with a decision body — the gateway reads the body, per
docs/policy-plane.md). These handlers just render an unexpected failure as JSON so
logs stay structured. Mirrors the model-gateway/rate-limiter-rpm errors.py shape
(ADR-003)."""

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
