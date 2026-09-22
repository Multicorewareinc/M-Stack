"""Auth router request/response schemas (design D3).

Pydantic-only so the module stays config-free at import. Password fields are capped at
`max_length=4096` so an anonymous caller cannot feed a multi-megabyte string into Argon2id — an
over-length password is rejected with 422 by schema validation BEFORE any hashing occurs.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=4096)


class ChangePasswordIn(BaseModel):
    current_password: str = Field(max_length=4096)
    new_password: str = Field(max_length=4096)


class UserOut(BaseModel):
    """Public user projection (`GET /api/auth/me`; `LoginResponse.user`)."""

    id: str
    email: str
    username: str
    display_name: str | None

    @classmethod
    def from_user(cls, user: Any) -> UserOut:
        return cls(
            id=str(user.id),
            email=user.email,
            username=user.username,
            display_name=user.display_name,
        )


class OrgOut(BaseModel):
    """Public organization projection (`LoginResponse.org`) — non-sensitive identity + name."""

    id: str
    name: str

    @classmethod
    def from_org(cls, org: Any) -> OrgOut:
        return cls(id=str(org.id), name=org.name)


class LoginResponse(BaseModel):
    """200 body for the JWT issuance path. The access token rides in the body; the refresh token is
    set as an HttpOnly cookie by the router, never here. `org` is null for a member-less principal
    (the SPA's "no organization yet" signal)."""

    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    user: UserOut
    org: OrgOut | None = None
    permissions: list[str] = Field(default_factory=list)
    must_change_password: bool = False


class MeResponse(BaseModel):
    """200 body for `GET /api/auth/me` — identity + resolved org + permissions (no access token)."""

    user: UserOut
    org: OrgOut | None = None
    permissions: list[str] = Field(default_factory=list)
    must_change_password: bool = False
