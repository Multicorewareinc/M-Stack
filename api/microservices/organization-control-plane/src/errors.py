"""Application errors and their handlers. Reuses the repo-wide JSON envelope
`{"error": {"message": ..., "type": ...}}` (ADR-003). Copied verbatim from admin-control-plane
(ADR-020: DB conventions reused, no shared library)."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class AppError(Exception):
    status_code = 500
    error_type = "internal_error"
    # A class-level fallback so credential/auth errors can be raised argument-free with a fixed,
    # branch-independent message (the no-enumeration invariant: every InvalidCredentialsError body
    # is byte-identical). Message-carrying errors (conflicts, not-found) still pass one explicitly.
    default_message = "Internal server error"

    def __init__(self, message: str | None = None, *, field: str | None = None) -> None:
        message = message if message is not None else self.default_message
        super().__init__(message)
        self.message = message
        # The request field this error pertains to (e.g. a uniqueness conflict on `name`),
        # when the raise site knows one. Omitted from the body when not set — most errors
        # aren't about a single field.
        self.field = field

    def body(self) -> dict:
        payload: dict = {"message": self.message, "type": self.error_type}
        if self.field is not None:
            payload["field"] = self.field
        return {"error": payload}


class UnauthorizedError(AppError):
    status_code = 401
    error_type = "unauthorized"


class ForbiddenError(AppError):
    status_code = 403
    error_type = "forbidden"
    default_message = "You do not have permission to perform this action."


class NotFoundError(AppError):
    status_code = 404
    error_type = "not_found"


class ConflictError(AppError):
    status_code = 409
    error_type = "conflict"


class UnprocessableError(AppError):
    status_code = 422
    error_type = "unprocessable"


class UpstreamError(AppError):
    # A dependency (e.g. Admin CP's internal API) was unreachable or returned an unexpected
    # status. Control operations fail closed with 502 rather than persisting unvalidated data.
    status_code = 502
    error_type = "upstream_error"


# ── Local auth errors (modules/auth, design D8) ──────────────────────────────────────────────
# Reuse the existing envelope (status_code + stable error_type string; no new `code` field). The
# three credential-failure branches (unknown email / no hash / wrong password) all raise
# InvalidCredentialsError argument-free, so the rendered body is byte-identical (no enumeration).


class InvalidCredentialsError(AppError):
    status_code = 401
    error_type = "invalid_credentials"
    default_message = "The email or password is incorrect."


class UserDeactivatedError(AppError):
    status_code = 401
    error_type = "user_deactivated"
    default_message = "Your account has been deactivated."


class LocalAuthDisabledError(AppError):
    status_code = 403
    error_type = "local_auth_disabled"
    default_message = "Local authentication is disabled on this deployment."


class TooManyAttemptsError(AppError):
    status_code = 429
    error_type = "too_many_attempts"
    default_message = "Too many failed login attempts. Try again later."

    def __init__(self, *, retry_after: int) -> None:
        super().__init__()
        # Remaining lock TTL, surfaced both in the body and as the Retry-After header (read by
        # install_error_handlers below via getattr).
        self.retry_after = retry_after


class PasswordChangeRequiredError(AppError):
    status_code = 403
    error_type = "password_change_required"
    default_message = "You must change your password before continuing."


class WeakPasswordError(AppError):
    status_code = 422
    error_type = "weak_password"
    default_message = "Password does not meet the minimum policy requirements."


class PasswordUnchangedError(AppError):
    status_code = 422
    error_type = "password_unchanged"
    default_message = "The new password must differ from the current password."


class SessionExpiredError(AppError):
    status_code = 401
    error_type = "session_expired"
    default_message = "Your session has expired. Please sign in again."


class OrgRequiredError(AppError):
    status_code = 403
    error_type = "org_required"
    default_message = "This request requires an organization context."


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle(_: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.error("app_error", extra={"status": exc.status_code, "type": exc.error_type})
        # An error may expose a `retry_after` (TooManyAttemptsError) — surface it as Retry-After.
        # Never log or echo any request body (no-secret-logging invariant): only status + type.
        retry_after = getattr(exc, "retry_after", None)
        headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
        return JSONResponse(status_code=exc.status_code, content=exc.body(), headers=headers)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_error")
        return JSONResponse(
            status_code=500,
            content={"error": {"message": "Internal server error", "type": "internal_error"}},
        )
