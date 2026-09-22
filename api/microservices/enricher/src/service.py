"""Token resolution — the enricher's core logic (ADR-030 AD-02/AD-03/D6).

Owns the estimation path that previously lived in rate-limiter-tpm (ADR-030 AD-02):
`_extract_text` + `count_via_tokenizer`. `resolve_usage` normalizes every response
event's `usage` to `{total_tokens, source[, reason][, prompt_tokens, completion_tokens]}`:

  1. provider  — a numeric `usage.total_tokens` on the source event (no tokenizer call);
     provider `prompt_tokens`/`completion_tokens` are preserved additively (D6).
  2. estimated — no numeric total but captured body text + a configured tokenizer:
     tokenize the completion text (undercounts prompt tokens by design, ADR-011 AD-06).
  3. none      — neither a numeric total nor a usable estimate; `total_tokens` stays null
     and `reason` records why (no_body | tokenizer_unset | tokenizer_error). Never fabricated.

httpx lives only here (ADR-003). All tokenizer failures are swallowed to a null count.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _extract_text(body) -> str | None:
    """Best-effort completion text from a captured, already-OpenAI-shaped response body
    (the gateway translates Anthropic responses upstream). Lifted from rate-limiter-tpm's
    consumer (ADR-030 AD-02). A miss just means estimation is not possible for this event."""
    if not isinstance(body, dict):
        return None
    choices = body.get("choices")
    if not isinstance(choices, list):
        return None
    parts: list[str] = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            parts.append(message["content"])
        elif isinstance(choice.get("text"), str):
            parts.append(choice["text"])
    text = "".join(parts)
    return text or None


async def count_via_tokenizer(client, tokenizer_url: str, text: str, timeout_ms: int) -> int | None:
    """Call the tokenizer service's POST /tokenize (ADR-014 contract, reused as-is). Never
    raises — any failure (unreachable, timeout, non-200, malformed body) returns None so the
    caller falls back to source="none" rather than erroring the consumer."""
    try:
        resp = await client.post(tokenizer_url, json={"text": text}, timeout=timeout_ms / 1000)
        if resp.status_code != 200:
            return None
        tokens = resp.json().get("tokens")
        return int(tokens) if _is_number(tokens) else None
    except Exception:
        logger.warning("tokenizer_call_failed", exc_info=True)
        return None


async def resolve_usage(event: dict, *, http_client, tokenizer_url: str, timeout_ms: int) -> dict:
    """Normalize `usage` for one response event. Returns a dict always carrying at least
    `total_tokens` (int|null) and `source` (provider|estimated|none)."""
    usage = event.get("usage")
    total = usage.get("total_tokens") if isinstance(usage, dict) else None

    if _is_number(total):
        out = {"total_tokens": int(total), "source": "provider"}
        # D6: preserve any provider-supplied breakdown additively (billing prices in/out).
        for field in ("prompt_tokens", "completion_tokens"):
            v = usage.get(field)
            if _is_number(v):
                out[field] = int(v)
        return out

    # Estimation path — a usage object without a numeric total falls through to here too.
    text = _extract_text(event.get("body"))
    if not text:
        return {"total_tokens": None, "source": "none", "reason": "no_body"}
    if not tokenizer_url or http_client is None:
        return {"total_tokens": None, "source": "none", "reason": "tokenizer_unset"}
    count = await count_via_tokenizer(http_client, tokenizer_url, text, timeout_ms)
    if count is None:
        return {"total_tokens": None, "source": "none", "reason": "tokenizer_error"}
    return {"total_tokens": count, "source": "estimated"}
