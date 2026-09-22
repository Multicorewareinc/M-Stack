"""Password hashing & policy primitive (leaf).

Pure functions over plain strings for local username/password auth. Argon2id is the hashing
default with a per-call salt; bcrypt is a verify-only compatibility branch selected off the stored
hash's own prefix (no bcrypt hashes exist today). This module:

- performs NO DB, Redis, or FastAPI work — it takes and returns plain values;
- builds NO Settings() at import and reads config only lazily inside functions, so importing it is
  side-effect-free (import contract);
- NEVER logs a raw password or a hash value and configures no logger.

Uses argon2-cffi + bcrypt directly (not passlib). Argon2id cost parameters come from Settings
(argon2_memory_cost / argon2_time_cost / argon2_parallelism), tunable via env without a code change.
"""

from __future__ import annotations

from functools import lru_cache

import bcrypt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from errors import WeakPasswordError

# bcrypt hash identifiers ($2a$ legacy, $2b$ current, $2y$ crypt-blowfish).
_BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$")

# A fixed throwaway password used only to build the dummy-verify hash. Hashing it is the same
# Argon2id work a real verify does; the value is meaningless and is never a real credential.
_DUMMY_PASSWORD = "persimmons.dummy.verify.password"


@lru_cache(maxsize=1)
def _hasher() -> PasswordHasher:
    """Build the Argon2id hasher from configured cost parameters, lazily (never at import).

    Cached so the (cheap) construction runs once per process; `_hasher.cache_clear()` lets tests
    rebuild after overriding a cost parameter.
    """
    from settings import Settings

    s = Settings()
    return PasswordHasher(
        memory_cost=s.argon2_memory_cost,
        time_cost=s.argon2_time_cost,
        parallelism=s.argon2_parallelism,
    )


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """The dummy hash used by dummy_verify, generated via _hasher so its cost stays in lock-step."""
    return _hasher().hash(_DUMMY_PASSWORD)


def hash_password(raw: str) -> str:
    """Return an Argon2id hash of `raw` (per-call salt embedded in the string)."""
    return _hasher().hash(raw)


def verify_password(raw: str, stored_hash: str) -> bool:
    """Return whether `raw` matches `stored_hash`.

    The algorithm is read from `stored_hash` itself: a bcrypt-format hash is verified through the
    bcrypt compatibility branch, anything else as Argon2id. A wrong password, an unrecognised /
    malformed hash, or an empty hash all return False — no exception propagates and no hash material
    is logged. There is no length-based early-out; the verification work runs on every call.
    """
    if stored_hash.startswith(_BCRYPT_PREFIXES):
        try:
            return bcrypt.checkpw(raw.encode("utf-8"), stored_hash.encode("utf-8"))
        except (ValueError, TypeError):
            return False
    try:
        return _hasher().verify(stored_hash, raw)
    except VerifyMismatchError:
        return False
    except (VerificationError, InvalidHashError, ValueError, TypeError):
        # A malformed/undecodable stored hash raises VerificationError (VerifyMismatchError's base):
        # treat every non-match as False, never raise, never log the hash.
        return False


def validate_password_policy(raw: str) -> None:
    """Raise WeakPasswordError if `raw` violates policy; else return None.

    The only rule in this phase is the configured minimum length (settings.password_min_length,
    default 12), read lazily at call time.
    """
    from settings import Settings

    if len(raw) < Settings().password_min_length:
        raise WeakPasswordError()


def dummy_verify() -> None:
    """Perform the same Argon2id verification work as a real failed verify.

    Call this on the unknown-email / no-stored-hash login branch so an existing and a non-existing
    account are indistinguishable by timing. The result is discarded; the work — one Argon2id verify
    at the configured cost — is the point.
    """
    try:
        _hasher().verify(_dummy_hash(), _DUMMY_PASSWORD)
    except Exception:  # pragma: no cover - dummy verify never surfaces a result
        pass
