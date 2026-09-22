"""API-key generation & hashing (leaf — imports only stdlib `secrets`/`hashlib`).

DELIBERATELY NOT `modules/auth/password.py` (Argon2id). An API key is a high-entropy machine
credential (~144 bits), not a guessable human password, and its hash doubles as a deterministic
lookup key (the stored `key_hash` column and the Redis `apikey:{hash}` cache suffix used by the
model-gateway verify path, SP-02). A slow, salted KDF is therefore the wrong tool here — the hash
MUST be deterministic. Reusing the password KDF would break the lookup and is explicitly forbidden
(ADR-028, reference API-KEY-MODULE.md §3 / §12.2).
"""

from __future__ import annotations

import hashlib
import secrets


def hash_key(raw: str) -> str:
    """Deterministic hex SHA-256 of the raw key — the only persisted form of the secret."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_raw_key() -> tuple[str, str, str]:
    """Mint a new key. Returns `(raw, prefix, key_hash)`.

    `raw` is returned to the caller exactly once and never persisted. `prefix` is a display/log-safe
    fragment (the first 11 chars, e.g. `sk-AbC12dE`) stored for identification. `key_hash` is the
    deterministic SHA-256 stored in the DB.
    """
    raw = "sk-" + secrets.token_urlsafe(24)
    return raw, raw[:11], hash_key(raw)
