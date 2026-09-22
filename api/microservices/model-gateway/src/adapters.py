"""Provider adapters. OpenAI is native (passthrough); Anthropic is translated
to/from the OpenAI shape. Pure functions so they unit-test without a network.
"""

from __future__ import annotations

import json
import time
import uuid

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 1024

_FINISH_REASON = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
}


def to_anthropic_request(payload: dict) -> dict:
    """OpenAI chat-completions body -> Anthropic /v1/messages body."""
    system_parts: list[str] = []
    messages: list[dict] = []
    for msg in payload.get("messages", []):
        role, content = msg.get("role"), msg.get("content", "")
        if role == "system":
            system_parts.append(content if isinstance(content, str) else json.dumps(content))
        else:
            messages.append({"role": role, "content": content})

    out: dict = {
        "model": payload["model"],
        "messages": messages,
        "max_tokens": payload.get("max_tokens", DEFAULT_MAX_TOKENS),
    }
    if system_parts:
        out["system"] = "\n\n".join(system_parts)
    for key in ("temperature", "top_p", "stop", "stream"):
        if key in payload:
            out["stop_sequences" if key == "stop" else key] = payload[key]
    return out


def to_openai_response(anthropic_json: dict, *, model: str) -> dict:
    """Anthropic message response -> OpenAI chat.completion object."""
    text = "".join(
        b.get("text", "") for b in anthropic_json.get("content", []) if b.get("type") == "text"
    )
    usage = anthropic_json.get("usage", {})
    prompt_tokens = usage.get("input_tokens", 0)
    completion_tokens = usage.get("output_tokens", 0)
    return {
        "id": anthropic_json.get("id", f"chatcmpl-{uuid.uuid4().hex}"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": _FINISH_REASON.get(anthropic_json.get("stop_reason"), "stop"),
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _openai_chunk(model: str, cid: str, *, delta: dict, finish_reason=None) -> bytes:
    obj = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(obj)}\n\n".encode()


async def translate_stream(anthropic_lines, *, model: str):
    """Anthropic SSE events -> OpenAI chat.completion.chunk SSE bytes."""
    cid = f"chatcmpl-{uuid.uuid4().hex}"
    yield _openai_chunk(model, cid, delta={"role": "assistant"})
    async for raw in anthropic_lines:
        line = (raw.decode() if isinstance(raw, bytes) else raw).strip()
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[len("data:"):].strip())
        except ValueError:
            continue
        etype = event.get("type")
        if etype == "content_block_delta":
            piece = event.get("delta", {}).get("text", "")
            if piece:
                yield _openai_chunk(model, cid, delta={"content": piece})
        elif etype == "message_delta":
            reason = event.get("delta", {}).get("stop_reason")
            yield _openai_chunk(model, cid, delta={}, finish_reason=_FINISH_REASON.get(reason, "stop"))
        elif etype == "message_stop":
            break
    yield b"data: [DONE]\n\n"
