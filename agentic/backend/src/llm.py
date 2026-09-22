"""
Chat model factory -- the only place a provider-specific class name
(ChatGroq, ChatAnthropic, ...) is allowed to appear. Orchestration code
must always go through get_chat_model(), never import a provider class
directly, so swapping models later means adding one branch here rather
than touching graph.py or anything downstream of it -- same reasoning as
the LLMClient interface in the mock_demo prototype, just built on top of
LangChain's already-common BaseChatModel interface instead of a
hand-rolled one.

Reads generic config, not a provider-specific name:
    LLM_PROVIDER  -- "groq" (default), "anthropic", "openai", "ollama", ...
    LLM_MODEL     -- provider-specific model name; every branch has its
                     own sensible default if this is unset
    LLM_BASE_URL  -- "openai" only: point at a self-hosted, OpenAI-API-
                     compatible server instead of the real OpenAI cloud
    LLM_USE_RESPONSES_API -- "openai" only: "true" to call /v1/responses
                     instead of /v1/chat/completions (gpt-oss on vLLM)

Each provider's own LangChain integration reads its own API key from its
own conventional env var (GROQ_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY,
...) automatically -- that's not duplicated here, and never routed
through a generic "LLM_API_KEY" that would just have to be re-mapped per
provider anyway.
"""

from __future__ import annotations

import os

from langchain_core.language_models.chat_models import BaseChatModel


# Without an explicit cap, some models (notably reasoning models like
# openai/gpt-oss-20b on Groq) can spend their entire response budget on
# internal reasoning and return an empty message -- no content, no tool
# call, nothing for the caller to act on. Confirmed in practice, not
# theoretical: this is what produced silent dead-ends in a real
# conversation before this was set. 2048 matches the bound the prototype
# needed for the same model family.
DEFAULT_MAX_TOKENS = 2048


def _with_replay_status(chat_openai):
    """A ChatOpenAI subclass for /v1/responses that replays the model's
    earlier replies in the shape vLLM accepts. LangChain sends a previous
    assistant message as an output item with no `status`, and vLLM's
    strict schema rejects the whole request with a 400 -- so every
    conversation broke on its second turn. The cloud API tolerates the
    missing field; setting it is harmless there too."""

    class ResponsesChatOpenAI(chat_openai):
        def _get_request_payload(self, input_, *, stop=None, **kwargs):
            payload = super()._get_request_payload(input_, stop=stop, **kwargs)
            for item in payload.get("input") or []:
                if (
                    isinstance(item, dict)
                    and item.get("type") == "message"
                    and item.get("role") == "assistant"
                    and "id" in item
                ):
                    item.setdefault("status", "completed")
            return payload

    return ResponsesChatOpenAI


def get_chat_model() -> BaseChatModel:
    """Constructs the configured chat model, reading LLM_PROVIDER/LLM_MODEL
    from the environment (see module docstring). Raises ValueError for an
    unrecognized provider rather than silently falling back to one."""
    provider = os.environ.get("LLM_PROVIDER", "groq").lower()
    model = os.environ.get("LLM_MODEL") or None

    if provider == "groq":
        from langchain_groq import ChatGroq

        return ChatGroq(model=model or "openai/gpt-oss-20b", max_tokens=DEFAULT_MAX_TOKENS)

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=model or "claude-opus-5", max_tokens=DEFAULT_MAX_TOKENS)

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        # LLM_BASE_URL is for a self-hosted, OpenAI-API-compatible server
        # (vLLM, llama.cpp's server, ...) rather than the real OpenAI
        # cloud -- same wire protocol, different host. The client still
        # requires *some* api_key to construct even when the server
        # doesn't check it (confirmed: raises OpenAIError otherwise), so
        # default to a placeholder only in that case -- a real
        # OPENAI_API_KEY is still required for the actual OpenAI cloud.
        base_url = os.environ.get("LLM_BASE_URL") or None
        api_key = os.environ.get("OPENAI_API_KEY") or ("not-needed" if base_url else None)
        # gpt-oss on vLLM returns real tool calls from /v1/responses with no
        # server flags, but from /v1/chat/completions only when the server
        # was started with --enable-auto-tool-choice --tool-call-parser
        # openai -- without them the arguments come back as plain text and
        # no plan is ever validated. Off by default: llama.cpp's server has
        # no /v1/responses at all.
        use_responses_api = os.environ.get("LLM_USE_RESPONSES_API", "").lower() in ("1", "true", "yes")
        chat_cls = _with_replay_status(ChatOpenAI) if use_responses_api else ChatOpenAI
        return chat_cls(
            model=model or "gpt-5",
            max_tokens=DEFAULT_MAX_TOKENS,
            base_url=base_url,
            api_key=api_key,
            use_responses_api=use_responses_api,
        )

    if provider == "ollama":
        from langchain_ollama import ChatOllama

        # Runs locally -- no API key, no rate limit -- but tool-calling
        # quality varies a lot by model. Confirm whatever LLM_MODEL is
        # set to actually supports tools before relying on it; a model
        # that doesn't just silently returns prose instead of calling
        # anything, which looks like it's working but never produces a
        # script. num_predict, not max_tokens -- Ollama's own name for
        # the same cap the other providers call max_tokens.
        #
        # num_ctx matters a lot here and the other providers don't need
        # an equivalent: Ollama defaults a model's context window to
        # 4096 (confirmed via `ollama ps`), but the real system prompt
        # plus all 4 tool schemas measures ~8200 tokens on its own,
        # before the conversation or a response even start -- the
        # request silently doesn't fit otherwise, which surfaced in
        # practice as the model hanging rather than a clean error.
        # 16384 leaves headroom for a multi-turn conversation too.
        return ChatOllama(
            model=model or "qwen2.5:7b",
            num_predict=DEFAULT_MAX_TOKENS,
            num_ctx=16384,
        )

    raise ValueError(
        f"Unknown LLM_PROVIDER '{provider}' -- add a branch in agentic/backend/src/llm.py "
        "for it (and install that provider's langchain integration package)"
    )
