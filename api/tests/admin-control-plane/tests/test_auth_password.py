"""Unit: modules/auth/password — Argon2id round-trip, bcrypt verify-only, policy, dummy_verify.

Maps spec 'Password hashing and policy' scenarios + task 9.1.
"""

from __future__ import annotations

import bcrypt
import pytest

from errors import WeakPasswordError
from modules.auth import password


def test_argon2_round_trip():
    h = password.hash_password("correct horse battery staple")
    assert h.startswith("$argon2id$")
    assert password.verify_password("correct horse battery staple", h) is True
    assert password.verify_password("wrong password entirely", h) is False


def test_bcrypt_verify_only():
    # A stored bcrypt hash verifies via the compatibility branch; new hashes are always Argon2id.
    stored = bcrypt.hashpw(b"bcrypt-legacy-pw", bcrypt.gensalt(rounds=4)).decode()
    assert stored.startswith("$2")
    assert password.verify_password("bcrypt-legacy-pw", stored) is True
    assert password.verify_password("nope", stored) is False
    # A freshly set password is Argon2id, never bcrypt.
    assert password.hash_password("bcrypt-legacy-pw").startswith("$argon2id$")


@pytest.mark.parametrize("bad", ["", "not-a-hash", "$argon2id$malformed", "$2b$broken"])
def test_verify_returns_false_never_raises(bad):
    assert password.verify_password("anything", bad) is False


def test_policy_rejects_short_password():
    with pytest.raises(WeakPasswordError):
        password.validate_password_policy("short")  # < 12 chars (default PASSWORD_MIN_LENGTH)
    # exactly 12 is allowed
    password.validate_password_policy("a" * 12)


def test_dummy_verify_runs_at_configured_cost():
    # dummy_verify performs one Argon2id verify against the throwaway hash; it never raises and
    # returns nothing. Its hash is built through the same _hasher, so cost stays in lock-step.
    assert password.dummy_verify() is None
    assert password._dummy_hash().startswith("$argon2id$")
