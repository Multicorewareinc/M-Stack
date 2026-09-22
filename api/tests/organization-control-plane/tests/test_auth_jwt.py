"""Unit: modules/auth/jwt — HS256 claim set, org claim, exp, unset-secret, no secret leak.

Maps spec 'HS256 session token issuance' scenarios + task 9.2.
"""

from __future__ import annotations

import jwt as pyjwt
import pytest
from pydantic import SecretStr

from modules.auth import jwt as auth_jwt

SECRET = SecretStr("unit-test-secret-abcdefghijklmnop")


def _decode(token):
    # Since the iss/aud fix, a token carries aud="org-control-plane" — PyJWT raises
    # InvalidAudienceError if `audience` isn't passed to decode() at all once the payload has an
    # `aud` claim, so this raw (non-decode_access_token) helper must pass it too.
    return pyjwt.decode(
        token, SECRET.get_secret_value(), algorithms=["HS256"], audience="org-control-plane"
    )


def test_claim_set_exact_with_org():
    token = auth_jwt.encode_access_token(
        sub="11111111-1111-1111-1111-111111111111",
        email="u@acme.com",
        org="22222222-2222-2222-2222-222222222222",
        secret=SECRET,
        ttl_seconds=3600,
    )
    claims = _decode(token)
    assert set(claims) == {"sub", "email", "role", "org", "iss", "aud", "iat", "exp", "jti"}
    assert claims["sub"] == "11111111-1111-1111-1111-111111111111"
    assert claims["email"] == "u@acme.com"
    assert claims["role"] == "org_user"  # D3a constant role
    assert claims["org"] == "22222222-2222-2222-2222-222222222222"
    assert claims["iss"] == "org-control-plane" and claims["aud"] == "org-control-plane"
    assert claims["exp"] == claims["iat"] + 3600


def test_org_claim_omitted_for_member_less():
    token = auth_jwt.encode_access_token(
        sub="1", email="u@acme.com", org=None, secret=SECRET, ttl_seconds=60
    )
    assert "org" not in _decode(token)


def test_unset_secret_raises():
    with pytest.raises(auth_jwt.JwtSecretUnsetError):
        auth_jwt.encode_access_token(
            sub="1", email="u@acme.com", org=None, secret=None, ttl_seconds=60
        )
    with pytest.raises(auth_jwt.JwtSecretUnsetError):
        auth_jwt.decode_access_token("whatever", secret=None)


def test_decode_round_trip_and_requires_claims():
    token = auth_jwt.encode_access_token(
        sub="1", email="u@acme.com", org=None, secret=SECRET, ttl_seconds=60
    )
    claims = auth_jwt.decode_access_token(token, secret=SECRET)
    assert claims["sub"] == "1"
    # Wrong secret → signature failure (a PyJWTError subclass).
    with pytest.raises(pyjwt.PyJWTError):
        auth_jwt.decode_access_token(token, secret=SecretStr("different-secret"))


def test_decode_rejects_wrong_audience():
    """Council review fix: decode_access_token pins audience="org-control-plane" — a same-secret,
    same-signature token minted for a different audience must still be rejected."""
    token = pyjwt.encode(
        {
            "sub": "1", "email": "u@acme.com", "role": "org_user",
            "iss": "admin-control-plane", "aud": "admin-control-plane",
            "iat": 0, "exp": 9999999999, "jti": "x",
        },
        SECRET.get_secret_value(),
        algorithm="HS256",
    )
    with pytest.raises(pyjwt.InvalidAudienceError):
        auth_jwt.decode_access_token(token, secret=SECRET)


def test_decode_rejects_missing_audience():
    """A token with no `aud` claim at all (e.g. minted by unpatched code) must be rejected, not
    silently accepted — `aud` is in the `require` list."""
    token = pyjwt.encode(
        {"sub": "1", "email": "u@acme.com", "role": "org_user", "iat": 0, "exp": 9999999999, "jti": "x"},
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
        auth_jwt.encode_access_token(
            sub="1", email="u@acme.com", org=None, secret=SECRET, ttl_seconds=60
        )
    assert "unit-test-secret" not in caplog.text
