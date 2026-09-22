"""Identity dependencies for the auth router.

Three identity primitives (mirrors the reference architecture's stateless-decode / load-and-gate /
load-only split, so a future consumer — e.g. the MG's own JWT verification, or a route that only
needs the principal and not a DB row — has a ready-made stateless seam):

- `current_user` — stateless: decodes the Bearer JWT into its claims only, no DB access. Enforces
  the `org` claim (ORG_REQUIRED on org routes) so that gate runs before any DB hit. Any decode
  failure is an opaque 401 (UnauthorizedError) — the caller never learns which check failed. An
  unset signing secret surfaces as a 500 (JwtSecretUnsetError), never a 401.
- `get_current_user` — `current_user` + load the row + the forced-rotation gate
  (PASSWORD_CHANGE_REQUIRED). Backs GET /api/auth/me.
- `get_authenticated_user` — decode + load the row, WITHOUT the org or rotation gate. The single
  sanctioned bypass, reserved for POST /api/auth/password so a must_change_password / org-less
  principal can still authenticate to rotate.
"""

from __future__ import annotations

import uuid

from fastapi import Request

from errors import (
    OrgRequiredError,
    PasswordChangeRequiredError,
    UnauthorizedError,
    UserDeactivatedError,
)
from modules.users.models import User

from .jwt import JwtSecretUnsetError, decode_access_token


def current_user(request: Request) -> dict:
    """Decode the Bearer token into its claims (no DB access). Raises UnauthorizedError (401) on
    any decode failure and OrgRequiredError (403) when the token carries no `org` claim — both
    before any row is loaded."""
    settings = request.app.state.settings
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise UnauthorizedError("Missing or malformed Authorization header")
    try:
        claims = decode_access_token(token.strip(), secret=settings.jwt_secret)
    except JwtSecretUnsetError:
        raise  # → 500, never an opaque 401 (misconfiguration, not a bad token)
    except Exception:
        # Opaque 401: signature/exp/sub/decode failures all render identically (no leak).
        raise UnauthorizedError("The access token is invalid") from None
    # Org route gate: a valid token with no `org` claim is a member-less principal → ORG_REQUIRED.
    if claims.get("org") is None:
        raise OrgRequiredError()
    return claims


def _decode_claims_no_org_gate(request: Request) -> dict:
    """Like `current_user` but WITHOUT the org gate — the un-gated `get_authenticated_user` bypass
    must still authenticate an org-less/un-rotated principal (change-password before they have or
    can prove org membership)."""
    settings = request.app.state.settings
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise UnauthorizedError("Missing or malformed Authorization header")
    try:
        return decode_access_token(token.strip(), secret=settings.jwt_secret)
    except JwtSecretUnsetError:
        raise
    except Exception:
        raise UnauthorizedError("The access token is invalid") from None


async def _load_user(request: Request, claims: dict) -> User:
    try:
        user_id = uuid.UUID(str(claims.get("sub")))
    except (ValueError, TypeError):
        raise UnauthorizedError("The access token is invalid") from None
    async with request.app.state.sessionmaker() as session:
        user = await session.get(User, user_id)
    if user is None:
        raise UnauthorizedError("The access token subject is unknown")
    return user


async def get_current_user(request: Request) -> User:
    claims = current_user(request)
    user = await _load_user(request, claims)
    # Active gate: a suspended/deactivated principal's still-valid access token must stop working
    # immediately, not merely at its own expiry — checked on every authenticated request, ahead of
    # the rotation gate (a deactivated user shouldn't be routed to change-password).
    if not user.active:
        raise UserDeactivatedError()
    # Forced-rotation gate: an un-rotated principal is confined to POST /api/auth/password.
    if user.must_change_password:
        raise PasswordChangeRequiredError()
    return user


async def get_authenticated_user(request: Request) -> User:
    claims = _decode_claims_no_org_gate(request)
    return await _load_user(request, claims)
