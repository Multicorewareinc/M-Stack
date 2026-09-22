"""Public `/api/auth` router — thin handlers that own the refresh cookie and delegate all logic to
the service. NOT behind require_admin_key (this is super-admin login, D8/8.3).

The refresh cookie is HttpOnly; Secure; SameSite=Strict, scoped to path /api/auth (reaches /refresh
and /logout only). The access token is returned in the body and NEVER placed in a cookie or URL.
No org / no permissions (super-admins are org-less).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse

from . import service
from .dependencies import get_authenticated_user, get_current_user
from .schemas import ChangePasswordIn, LoginRequest, LoginResponse, MeResponse, UserOut

router = APIRouter(prefix="/api/auth", tags=["auth"])

REFRESH_COOKIE_NAME = "refresh"
REFRESH_COOKIE_PATH = "/api/auth"


def _client_ip(request: Request, trusted_proxy_count: int) -> str:
    """Resolve the client IP for lockout keying (design D3). With trusted_proxy_count > 0, take the
    entry that many hops from the right of X-Forwarded-For; else the socket peer."""
    peer = request.client.host if request.client else "unknown"
    if trusted_proxy_count > 0:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            if parts:
                idx = len(parts) - trusted_proxy_count
                return parts[max(0, min(idx, len(parts) - 1))]
    return peer


def _set_refresh_cookie(response: Response, raw_refresh: str, max_age: int) -> None:
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        raw_refresh,
        max_age=max_age,
        httponly=True,
        secure=True,
        samesite="strict",
        path=REFRESH_COOKIE_PATH,
    )


def _login_body(outcome: service.LoginOutcome) -> dict:
    issued = outcome.issued
    return LoginResponse(
        access_token=issued.access_token,
        expires_in=issued.expires_in,
        user=UserOut.from_user(outcome.user),
        must_change_password=outcome.must_change_password,
    ).model_dump()


@router.post("/login")
async def login(payload: LoginRequest, request: Request) -> Response:
    settings = request.app.state.settings
    ip = _client_ip(request, settings.trusted_proxy_count)
    outcome = await service.login_local(
        request.app.state.sessionmaker, settings, payload.email, payload.password, ip=ip
    )
    response = JSONResponse(status_code=200, content=_login_body(outcome))
    _set_refresh_cookie(response, outcome.issued.refresh_token, outcome.issued.refresh_max_age)
    return response


@router.post("/refresh")
async def refresh(request: Request) -> Response:
    settings = request.app.state.settings
    raw = request.cookies.get(REFRESH_COOKIE_NAME)
    outcome = await service.refresh_session(request.app.state.sessionmaker, settings, raw)
    response = JSONResponse(status_code=200, content=_login_body(outcome))
    _set_refresh_cookie(response, outcome.issued.refresh_token, outcome.issued.refresh_max_age)
    return response


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request) -> Response:
    raw = request.cookies.get(REFRESH_COOKIE_NAME)
    await service.logout_session(request.app.state.sessionmaker, raw)
    response = Response(status_code=204)
    response.delete_cookie(REFRESH_COOKIE_NAME, path=REFRESH_COOKIE_PATH)
    return response


@router.get("/me")
async def me(request: Request, user=Depends(get_current_user)) -> Response:
    body = MeResponse(
        user=UserOut.from_user(user),
        must_change_password=user.must_change_password,
    ).model_dump()
    return JSONResponse(status_code=200, content=body)


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    payload: ChangePasswordIn, request: Request, user=Depends(get_authenticated_user)
) -> Response:
    await service.change_password(
        request.app.state.sessionmaker,
        request.app.state.settings,
        user.id,
        payload.current_password,
        payload.new_password,
    )
    return Response(status_code=204)
