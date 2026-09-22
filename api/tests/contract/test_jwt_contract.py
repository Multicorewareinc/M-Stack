"""Cross-service JWT wire-contract test (SP-04; add-jwt-cross-service-contract).

Admin CP and Org CP each mint the platform JWT from their OWN duplicated encoder (AD-04, no shared
library). This is the single source of truth that pins the two encoders to one wire contract, so any
drift — a renamed/added/removed claim or a changed algorithm — fails here rather than silently
breaking auth platform-wide (the Model Gateway will later verify either token uniformly).

Both `modules/auth/jwt.py` files are pure leaves (stdlib + PyJWT + SecretStr, no service imports), so
they load in-process via importlib with no sys.path edits, stubbing, or subprocess (design D1). Pure
offline — no DB, no running services, no Docker (design D4).

Council review fix: both services verify under the SAME shared HS256 secret, so a token minted by
one was previously signature-valid at the other too — the ONLY thing stopping it from being
ACCEPTED was the incidental fact that each CP's `sub` lookup targets its own users table (a foreign
sub simply 404s). `iss`/`aud` are now REQUIRED, per-tier-constant claims, and each decoder pins
`audience=<its own tier>` — turning that accident into a structural rejection. The golden contract
below is updated accordingly; `test_cross_tier_tokens_are_rejected_despite_shared_secret` pins the
new (correct) behavior, replacing the old test that asserted the opposite.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import jwt as pyjwt
from pydantic import SecretStr

# Golden contract (design D2, extended by the council review iss/aud fix).
REQUIRED = {"sub", "email", "role", "iss", "aud", "iat", "exp", "jti"}
ALG = "HS256"
SECRET = SecretStr("shared-platform-jwt-secret-0123456789abcdef")
TTL = 3600
ORG_AUDIENCE = "org-control-plane"
ADMIN_AUDIENCE = "admin-control-plane"

# Repo layout: this file is api/tests/contract/test_jwt_contract.py → repo root is three parents up.
_ROOT = Path(__file__).resolve().parents[3]
_ORG_JWT = _ROOT / "api/microservices/organization-control-plane/src/modules/auth/jwt.py"
_ADMIN_JWT = _ROOT / "api/microservices/admin-control-plane/src/modules/auth/jwt.py"


def _load(name: str, path: Path):
    assert path.is_file(), f"encoder not found: {path}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


org_jwt = _load("org_jwt", _ORG_JWT)
admin_jwt = _load("admin_jwt", _ADMIN_JWT)

ORG_ID = "11111111-1111-1111-1111-111111111111"


def _org_token() -> str:
    return org_jwt.encode_access_token(
        sub="org-user-id", email="user@org.local", org=ORG_ID, secret=SECRET, ttl_seconds=TTL
    )


def _admin_token() -> str:
    return admin_jwt.encode_access_token(
        sub="admin-id", email="admin@site.local", secret=SECRET, ttl_seconds=TTL
    )


def _claims(token: str, *, audience: str) -> dict:
    return pyjwt.decode(token, SECRET.get_secret_value(), algorithms=[ALG], audience=audience)


def test_both_headers_declare_hs256():
    for token in (_org_token(), _admin_token()):
        assert pyjwt.get_unverified_header(token)["alg"] == ALG


def test_org_token_has_required_claims_plus_org():
    c = _claims(_org_token(), audience=ORG_AUDIENCE)
    assert set(c.keys()) == REQUIRED | {"org"}
    assert c["org"] == ORG_ID
    assert c["role"] == "org_user"
    assert c["iss"] == ORG_AUDIENCE and c["aud"] == ORG_AUDIENCE
    assert c["exp"] == c["iat"] + TTL


def test_admin_token_has_exactly_required_claims_no_org():
    c = _claims(_admin_token(), audience=ADMIN_AUDIENCE)
    assert set(c.keys()) == REQUIRED  # exact set → an ADDED claim would fail (drift guard)
    assert "org" not in c
    assert c["role"] == "super_admin"
    assert c["iss"] == ADMIN_AUDIENCE and c["aud"] == ADMIN_AUDIENCE
    assert c["exp"] == c["iat"] + TTL


def test_claim_shapes_match_except_org():
    org_keys = set(_claims(_org_token(), audience=ORG_AUDIENCE).keys())
    admin_keys = set(_claims(_admin_token(), audience=ADMIN_AUDIENCE).keys())
    # Identical after removing the org key — neither side added or renamed a claim.
    assert org_keys - {"org"} == admin_keys
    assert org_keys - admin_keys == {"org"}


def test_cross_tier_tokens_are_rejected_despite_shared_secret():
    """Both services verify under the SAME shared HS256 secret, so a token minted by one is still
    SIGNATURE-valid at the other — but each decoder now pins `audience=<its own tier>`, so a
    cross-tier token is rejected at the `aud` check (council review fix: this used to succeed,
    relying only on the incidental fact that `sub` lookups target different tables)."""
    org_token = _org_token()
    admin_token = _admin_token()
    try:
        admin_jwt.decode_access_token(org_token, secret=SECRET)
        raised = False
    except pyjwt.InvalidAudienceError:
        raised = True
    assert raised, "Admin CP must reject an Org CP-minted token on audience mismatch"

    try:
        org_jwt.decode_access_token(admin_token, secret=SECRET)
        raised = False
    except pyjwt.InvalidAudienceError:
        raised = True
    assert raised, "Org CP must reject an Admin CP-minted token on audience mismatch"


def test_same_tier_tokens_still_decode():
    # The audience fix must not break normal same-tier verification.
    assert org_jwt.decode_access_token(_org_token(), secret=SECRET)["sub"] == "org-user-id"
    assert admin_jwt.decode_access_token(_admin_token(), secret=SECRET)["sub"] == "admin-id"


def test_required_claim_set_is_pinned_exactly():
    # Guard the golden set itself so a future edit to REQUIRED is a deliberate, visible change.
    assert REQUIRED == {"sub", "email", "role", "iss", "aud", "iat", "exp", "jti"}
