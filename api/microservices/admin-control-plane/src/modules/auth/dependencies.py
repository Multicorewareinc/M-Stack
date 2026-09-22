"""Identity dependencies for the auth router — org-less super-admin variant.

Three identity primitives (mirrors Org CP's split — a future consumer, e.g. a route that only needs
the principal and not a DB row, has a ready-made stateless seam):

- `current_user` — stateless: decodes the Bearer JWT into its claims only, no DB access. NO org gate
  (super-admins are org-less). Any decode failure is an opaque 401 (UnauthorizedError). An unset
  signing secret surfaces as a 500 (JwtSecretUnsetError), never a 401.
- `get_current_user` — `current_user` + load the row + the forced-rotation gate
  (PASSWORD_CHANGE_REQUIRED). Backs GET /api/auth/me.
- `get_authenticated_user` — decode + load the row, WITHOUT the rotation gate. The single sanctioned
  bypass, reserved for POST /api/auth/password so a must_change_password principal can still
  authenticate to rotate.
"""

from __future__ import annotations

import uuid

from fastapi import Request

from errors import PasswordChangeRequiredError, UnauthorizedError, UserDeactivatedError
from modules.auth.models import AdminUser

from .jwt import JwtSecretUnsetError, decode_access_token


def current_user(request: Request) -> dict:
    """Decode the Bearer token into its claims (no DB access). Raises UnauthorizedError (401) on
    any decode failure. Super-admins are org-less, so there is no org gate here."""
    settings = request.app.state.settings
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise UnauthorizedError("Missing or malformed Authorization header")
    try:
        return decode_access_token(token.strip(), secret=settings.jwt_secret)
    except JwtSecretUnsetError:
        raise  # → 500, never an opaque 401 (misconfiguration, not a bad token)
    except Exception:
        # Opaque 401: signature/exp/sub/decode failures all render identically (no leak).
        raise UnauthorizedError("The access token is invalid") from None


async def _load_user(request: Request, claims: dict) -> AdminUser:
    try:
        user_id = uuid.UUID(str(claims.get("sub")))
    except (ValueError, TypeError):
        raise UnauthorizedError("The access token is invalid") from None
    async with request.app.state.sessionmaker() as session:
        user = await session.get(AdminUser, user_id)
    if user is None:
        raise UnauthorizedError("The access token subject is unknown")
    return user


async def get_current_user(request: Request) -> AdminUser:
    claims = current_user(request)
    user = await _load_user(request, claims)
    # Active gate: a deactivated super-admin's still-valid access token must stop working
    # immediately, not merely at its own expiry. There is no admin-user deactivation endpoint yet
    # (`active` is set-once at seed time), but the check is defense-in-depth against any future
    # write path and mirrors Org CP's identical gate.
    if not user.active:
        raise UserDeactivatedError()
    # Forced-rotation gate: an un-rotated principal is confined to POST /api/auth/password.
    if user.must_change_password:
        raise PasswordChangeRequiredError()
    return user


async def get_authenticated_user(request: Request) -> AdminUser:
    claims = current_user(request)
    return await _load_user(request, claims)
