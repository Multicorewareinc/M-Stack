"""A chat model as the last ranking step: it reads the question and the
best candidates together and names the ones that help answer it, most
useful first.

The cross-encoder before it scores each candidate alone, and a small one
scores nearly everything as relevant, so it can't tell the passage that
answers a question from one that only shares its subject. Scored against
the grounding questions with RAGAS's own context-precision judge, this
step raised precision from 0.82 to 0.90, improving 21 of 41 questions.

It costs one chat completion per search, a few seconds; any failure keeps
the order the cross-encoder gave.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import httpx

PROMPT = """You rank reference passages for a question about {subject}.

Question: {question}

Passages:
{passages}

Which passages contain information needed to answer the question? Return their numbers, most useful first, \
as JSON: {{"useful": [numbers]}}. Leave out any passage that does not help answer it."""

_JSON = re.compile(r"\{.*\}", re.DOTALL)


class LLMRerankError(RuntimeError):
    """The model could not be reached, or its answer could not be read."""


class _Candidate(Protocol):
    @property
    def path(self) -> str: ...
    @property
    def start(self) -> int: ...
    @property
    def end(self) -> int: ...
    @property
    def context(self) -> str: ...
    @property
    def text(self) -> str: ...


@dataclass(frozen=True)
class LLMRerankConfig:
    url: str  # an OpenAI-compatible /v1/chat/completions
    model: str
    timeout_seconds: float = 20.0
    subject: str = "the Multistack platform"
    # Enough of a long passage to say what it is; fifteen whole ones
    # would make a slow request slower.
    max_passage_chars: int = 1500
    reasoning_effort: str = "low"
    max_tokens: int = 2000


class LLMReranker:
    def __init__(self, config: LLMRerankConfig, *, client: httpx.Client | None = None) -> None:
        self.config = config
        self._client = client or httpx.Client(timeout=config.timeout_seconds)

    def useful(self, question: str, candidates: Sequence[_Candidate]) -> list[int]:
        """Indices into `candidates` of the ones that help answer
        `question`, most useful first. May be empty."""
        if not candidates:
            return []
        c = self.config
        shown = "\n\n".join(
            f"[{n}] {p.path}:{p.start}-{p.end} ({p.context})\n{p.text[: c.max_passage_chars]}"
            for n, p in enumerate(candidates, start=1)
        )
        body = {
            "model": c.model,
            "messages": [
                {
                    "role": "user",
                    "content": PROMPT.format(subject=c.subject, question=question, passages=shown),
                }
            ],
            "max_tokens": c.max_tokens,
            "reasoning_effort": c.reasoning_effort,
        }
        try:
            response = self._client.post(c.url, json=body)
        except httpx.HTTPError as e:
            raise LLMRerankError(f"could not reach the model at {c.url}: {e}") from e
        if response.status_code != 200:
            raise LLMRerankError(
                f"the model answered {response.status_code}: {response.text[:300]}"
            )
        try:
            reply = response.json()["choices"][0]["message"]["content"] or ""
            match = _JSON.search(reply)
            numbers = json.loads(match.group(0))["useful"] if match else None
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise LLMRerankError(f"the model's answer could not be read: {e}") from e
        if not isinstance(numbers, list):
            raise LLMRerankError(
                f"no list of passage numbers in the model's answer: {reply[:200]!r}"
            )
        order: list[int] = []
        for n in numbers:
            if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= len(candidates):
                if n - 1 not in order:
                    order.append(n - 1)
        return order
