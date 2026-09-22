"""Unit: modules/auth/jwt — HS256 claim set (org-less), role=super_admin, exp, unset-secret,
no secret leak. Maps spec 'HS256 session token issuance (org-less)' scenarios + task 9.2."""

from __future__ import annotations

import jwt as pyjwt
import pytest
from pydantic import SecretStr

from modules.auth import jwt as auth_jwt

SECRET = SecretStr("unit-test-secret-abcdefghijklmnop")


def _decode(token):
    # Since the iss/aud fix, a token carries aud="admin-control-plane" — PyJWT raises
    # InvalidAudienceError if `audience` isn't passed to decode() at all once the payload has an
    # `aud` claim, so this raw (non-decode_access_token) helper must pass it too.
    return pyjwt.decode(
        token, SECRET.get_secret_value(), algorithms=["HS256"], audience="admin-control-plane"
    )


def test_claim_set_exact_no_org():
    token = auth_jwt.encode_access_token(
        sub="11111111-1111-1111-1111-111111111111",
        email="super@platform.local",
        secret=SECRET,
        ttl_seconds=3600,
    )
    claims = _decode(token)
    # Exactly the org-less claim set — NO `org` claim (super-admins are org-less).
    assert set(claims) == {"sub", "email", "role", "iss", "aud", "iat", "exp", "jti"}
    assert "org" not in claims
    assert claims["sub"] == "11111111-1111-1111-1111-111111111111"
    assert claims["email"] == "super@platform.local"
    assert claims["role"] == "super_admin"  # D1 constant role
    assert claims["iss"] == "admin-control-plane" and claims["aud"] == "admin-control-plane"
    assert claims["exp"] == claims["iat"] + 3600


def test_role_constant_super_admin():
    token = auth_jwt.encode_access_token(
        sub="1", email="s@platform.local", secret=SECRET, ttl_seconds=60
    )
    assert _decode(token)["role"] == "super_admin"


def test_unset_secret_raises():
    with pytest.raises(auth_jwt.JwtSecretUnsetError):
        auth_jwt.encode_access_token(sub="1", email="s@x", secret=None, ttl_seconds=60)
    with pytest.raises(auth_jwt.JwtSecretUnsetError):
        auth_jwt.decode_access_token("whatever", secret=None)


def test_decode_round_trip_and_requires_claims():
    token = auth_jwt.encode_access_token(
        sub="1", email="s@x", secret=SECRET, ttl_seconds=60
    )
    claims = auth_jwt.decode_access_token(token, secret=SECRET)
    assert claims["sub"] == "1"
    # Wrong secret → signature failure (a PyJWTError subclass).
    with pytest.raises(pyjwt.PyJWTError):
        auth_jwt.decode_access_token(token, secret=SecretStr("different-secret"))


def test_decode_rejects_wrong_audience():
    """Council review fix: decode_access_token pins audience="admin-control-plane" — a
    same-secret, same-signature token minted for a different audience must still be rejected."""
    now = pyjwt.encode(
        {
            "sub": "1", "email": "s@x", "role": "super_admin",
            "iss": "org-control-plane", "aud": "org-control-plane",
            "iat": 0, "exp": 9999999999, "jti": "x",
        },
        SECRET.get_secret_value(),
        algorithm="HS256",
    )
    with pytest.raises(pyjwt.InvalidAudienceError):
        auth_jwt.decode_access_token(now, secret=SECRET)


def test_decode_rejects_missing_audience():
    """A token with no `aud` claim at all (e.g. minted by unpatched code) must be rejected, not
    silently accepted — `aud` is in the `require` list."""
    token = pyjwt.encode(
        {"sub": "1", "email": "s@x", "role": "super_admin", "iat": 0, "exp": 9999999999, "jti": "x"},
        SECRET.get_secret_value(),
        algorithm="HS256",
    )
    with pytest.raises(pyjwt.MissingRequiredClaimError):
        auth_jwt.decode_access_token(token, secret=SECRET)


def test_secret_never_in_repr_or_logs(caplog):
    # SecretStr masks its value in repr/str; and the module never logs the secret.
    assert "unit-test-secret" not in repr(SECRET)
    assert "unit-test-secret" not in str(SECRET)
    with caplog.at_level("DEBUG"):
        auth_jwt.encode_access_token(sub="1", email="s@x", secret=SECRET, ttl_seconds=60)
    assert "unit-test-secret" not in caplog.text
