"""HS256 access-token encode/decode (leaf).

Signs and verifies the platform JWT claim set `{sub, email, role, org?, iss, aud, iat, exp, jti}`
with a single HS256 secret shared with Admin CP (no key distribution). PyJWT only. The secret is
read from a `SecretStr` and is NEVER logged; an unset secret raises (surfacing as a 500) rather
than emitting an unsigned token.

`iss`/`aud` are fixed per-tier constants (council review fix): because both control planes verify
under the SAME shared HS256 secret, a token minted by either is signature-valid at the other —
before this fix, the only thing stopping a cross-tier token from being ACCEPTED was the incidental
fact that each CP's `sub` lookup targets its own users table (a foreign sub simply 404s). Pinning
`aud` to this tier's constant and requiring it on decode makes that an explicit, structural check
instead of an accident of two different tables.

Leaf: no DB or FastAPI imports. Callers pass the secret + TTL from Settings so the module stays
config-free at import.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt as pyjwt
from pydantic import SecretStr

_ALGORITHM = "HS256"
ROLE_CLAIM = "org_user"  # ponytail: constant role — the users table has no platform-role column.
# Upgrade path (D3a): derive org_admin/org_user from RBAC role assignments when a platform-tier
# distinction is actually needed. Keeping it constant pins the JWT claim shape for SP-02/SP-04.
ISSUER = "org-control-plane"
AUDIENCE = "org-control-plane"


class JwtSecretUnsetError(RuntimeError):
    """The HS256 signing secret is not configured — a deployment misconfiguration, never an
    unsigned token. Surfaces as a 500 (caught by the app's catch-all handler), and the secret
    itself is never referenced in the message."""


def _require_secret(secret: SecretStr | None) -> str:
    if secret is None:
        raise JwtSecretUnsetError("JWT signing key is not configured.")
    return secret.get_secret_value()


def encode_access_token(
    *,
    sub: str,
    email: str,
    org: str | None,
    secret: SecretStr | None,
    ttl_seconds: int,
) -> str:
    """Sign an HS256 access token for a principal.

    Claims: `sub` (user id str), `email`, `role` (constant "org_user"), `iat`, `exp`
    (= iat + ttl_seconds), `jti` (unique), and — when resolved — `org` (org id str). Raises
    JwtSecretUnsetError (→ 500) when `secret` is None; the secret is read only here and never logged.
    """
    key = _require_secret(secret)
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": sub,
        "email": email,
        "role": ROLE_CLAIM,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    if org is not None:
        claims["org"] = org
    return pyjwt.encode(claims, key, algorithm=_ALGORITHM)


def decode_access_token(token: str, *, secret: SecretStr | None) -> dict[str, Any]:
    """Verify an HS256 access token and return its claims.

    Requires a valid signature and the `exp`/`sub` claims, AND that `aud` equals this tier's fixed
    AUDIENCE — a token minted by Admin CP (whose tokens carry `aud="admin-control-plane"`) is
    rejected here even though both CPs share one HS256 secret. Raises `jwt.PyJWTError` subclasses on
    any validation failure (the identity dependency maps all of them to an opaque 401) and
    JwtSecretUnsetError (→ 500) when the secret is unset.
    """
    key = _require_secret(secret)
    return pyjwt.decode(
        token,
        key,
        algorithms=[_ALGORITHM],
        audience=AUDIENCE,
        options={"require": ["exp", "sub", "aud"]},
    )
