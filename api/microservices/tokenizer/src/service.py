"""Token counting. A name-keyed registry of backends (ADR-014); exactly one wired in this
build (`cl100k_base` via `tiktoken`). Adding a second backend is additive: a new registry
entry, no interface change.

`tiktoken` is imported lazily inside the encoder function so the module (and offline tests,
which inject a fake registry) never need it installed.
"""

from __future__ import annotations

from collections.abc import Callable

from errors import UnknownTokenizerError


def _encode_cl100k_base(text: str) -> int:
    import tiktoken  # lazy: offline tests inject a fake registry instead

    enc = tiktoken.get_encoding("cl100k_base")
    return len(enc.encode(text))


TOKENIZERS: dict[str, Callable[[str], int]] = {
    "cl100k_base": _encode_cl100k_base,
}


class TokenizerService:
    def __init__(self, *, tokenizers: dict[str, Callable[[str], int]], default: str) -> None:
        self._tokenizers = tokenizers
        self._default = default

    def count(self, text: str, tokenizer: str | None) -> tuple[int, str]:
        """Resolve the backend (named, or the configured default) and count `text`.
        Empty text is valid (0 tokens back). An unregistered name is rejected, never
        silently substituted for the default."""
        name = tokenizer or self._default
        fn = self._tokenizers.get(name)
        if fn is None:
            raise UnknownTokenizerError(f"unknown tokenizer: {name!r}")
        return fn(text), name
