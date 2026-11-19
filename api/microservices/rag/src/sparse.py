"""Exact-word search, expressed as sparse vectors for the vector store.

The embedding model matches meaning and blurs precise names: a question
containing `event_backbone_url` finds chunks about event backbones, not
necessarily the one that defines that field. Word matching is the other
half. Identifiers are indexed whole and as their parts, so the exact name
and a looser "backbone url" both land on it.

The store does the BM25 arithmetic. A chunk's vector holds, per word, the
saturated and length-normalised term frequency; a question's vector holds
1.0 per word; the store multiplies in each word's inverse document
frequency (collection modifier `idf`) when it takes the dot product.
"""

from __future__ import annotations

import re
import zlib
from collections import Counter

K1 = 1.2
B = 0.75

_WORD = re.compile(r"[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")

# Words that carry no signal in a question or in code comments.
STOPWORDS = frozenset(
    "a an the of to is are was were be been in on for and or it its this that these those with as by at "
    "from do does did how what why when which who can could should would i my me we our you your if then "
    "so than there here".split()
)


def _stem(token: str) -> str:
    """Plural 's' only. Applied to documents and questions alike, so
    "layers" finds "layer"; it need not be linguistically right."""
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for word in _WORD.findall(text):
        parts = [w for piece in word.split("_") for w in _CAMEL.findall(piece)] or [word]
        candidates = [word, *parts] if len(parts) > 1 else parts
        for candidate in candidates:
            token = _stem(candidate.lower())
            if len(token) > 1 and token not in STOPWORDS:
                tokens.append(token)
    return tokens


def term_id(token: str) -> int:
    """A word's index in a sparse vector: 32 bits, the store's limit."""
    return zlib.crc32(token.encode())


def average_length(token_lists: list[list[str]]) -> float:
    return (sum(len(t) for t in token_lists) / len(token_lists)) if token_lists else 0.0


def document_vector(tokens: list[str], average: float) -> dict:
    """`{"indices": [...], "values": [...]}` for one chunk."""
    counts = Counter(term_id(t) for t in tokens)
    norm = (1 - B + B * len(tokens) / average) if average else 1.0
    indices = sorted(counts)
    return {
        "indices": indices,
        "values": [counts[i] * (K1 + 1) / (counts[i] + K1 * norm) for i in indices],
    }


def query_vector(question: str) -> dict:
    indices = sorted({term_id(t) for t in tokenize(question)})
    return {"indices": indices, "values": [1.0] * len(indices)}
