"""API-key hashing for the verify path (leaf — stdlib `hashlib` only).

This is an INTENTIONAL byte-for-byte copy of the Organization Control Plane's `hash_key`
(`modules/api_keys/keygen.py`). The project has no shared library (`openspec/project.md`), so the
verifier (this gateway) and the minter (Org CP) each carry their own copy; a cross-service parity
test asserts they produce the identical digest for the same raw. Deterministic hex SHA-256 — the raw
`sk-…` is a high-entropy machine credential and its hash doubles as the `apikey:{hash}` cache/lookup
key (ADR-028), so it must NOT be a slow/salted KDF.
"""

from __future__ import annotations

import hashlib


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
