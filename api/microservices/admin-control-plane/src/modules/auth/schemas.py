"""Auth router request/response schemas (design D3) — org-less super-admin variant.

Pydantic-only so the module stays config-free at import. Password fields are capped at
`max_length=4096` so an anonymous caller cannot feed a multi-megabyte string into Argon2id — an
over-length password is rejected with 422 by schema validation BEFORE any hashing occurs. There is
NO org and NO permissions projection (super-admins are org-less).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .jwt import ROLE_CLAIM


class LoginRequest(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=4096)


class ChangePasswordIn(BaseModel):
    current_password: str = Field(max_length=4096)
    new_password: str = Field(max_length=4096)


class UserOut(BaseModel):
    """Public super-admin projection (`GET /api/auth/me`; `LoginResponse.user`) — id, email, and the
    constant super-admin `role`. No org (super-admins are org-less)."""

    id: str
    email: str
    role: str = ROLE_CLAIM

    @classmethod
    def from_user(cls, user: Any) -> UserOut:
        return cls(id=str(user.id), email=user.email, role=ROLE_CLAIM)


class LoginResponse(BaseModel):
    """200 body for the JWT issuance path. The access token rides in the body; the refresh token is
    set as an HttpOnly cookie by the router, never here. No org, no permissions (super-admins are
    org-less)."""

    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    user: UserOut
    must_change_password: bool = False


class MeResponse(BaseModel):
    """200 body for `GET /api/auth/me` — the super-admin identity (no access token, no org)."""

    user: UserOut
    must_change_password: bool = False
