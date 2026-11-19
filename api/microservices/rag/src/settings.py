"""Service configuration, read from the environment (and an optional .env)."""

from __future__ import annotations

from embeddings import (
    DEFAULT_DOCUMENT_TEMPLATE,
    DEFAULT_QUERY_INSTRUCTION,
    DEFAULT_QUERY_TEMPLATE,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    log_level: str = "INFO"

    # Bearer token callers must send. Unset fails closed with a 500 (a
    # deployment mistake, never a bypass), like the other services.
    service_api_key: str = ""

    # The embedding server (OpenAI-compatible /v1/embeddings). The model
    # here must be the one the collection was indexed with.
    embedding_url: str = ""
    embedding_model: str = ""
    embedding_batch_size: int = 32
    # Indexing sends batches and can wait; a search is on a caller's clock,
    # and a caller that has given up gains nothing from a later answer.
    embedding_timeout_seconds: float = 60.0
    search_embedding_timeout_seconds: float = 4.0
    embedding_dimension: int = 1024
    query_instruction: str = DEFAULT_QUERY_INSTRUCTION
    # How the model wants a question and a document wrapped (embeddings.py
    # lists a few). The defaults are Qwen3-Embedding's.
    query_template: str = DEFAULT_QUERY_TEMPLATE
    document_template: str = DEFAULT_DOCUMENT_TEMPLATE
    embedding_max_input_tokens: int = 4096

    # Chunking: the longest chunk, and what consecutive parts of a longer
    # unit share. Changing either re-chunks, and so re-embeds, what they cut.
    chunk_max_tokens: int = 512
    chunk_overlap_tokens: int = 64

    # The vector database this service owns.
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    qdrant_collection: str = "multistack_reference"
    qdrant_timeout_seconds: float = 10.0

    # Search. Chosen by scoring against the grounding questions (README).
    top_k: int = 8
    # The whole reference block: instructions, examples and passages.
    budget_tokens: int = 6000
    pool: int = 50  # candidates taken from each ranking before merging
    rrf_k: int = 60
    stub_penalty: float = 0.5  # multiplier on short "class X" chunks
    capability_boost: float = 1.0  # multiplier on the capabilities the caller names
    # Of top_k, how many are held for the named capabilities' own skill
    # notes. Code that matches a question on both rankings outscores a rule
    # that matches it on one, and the rule is often what the answer needs.
    skill_slots: int = 2

    # The reranker (a cross-encoder behind vLLM's /v1/rerank). Unset, the
    # hybrid ranking is final. It reorders the top `rerank_candidates` of
    # that ranking; any failure falls back to the hybrid order.
    rerank_url: str = ""
    rerank_model: str = ""
    # Reranking cost grows with every candidate sent; the answers that
    # matter sit near the top of the merged ranking already.
    rerank_candidates: int = 10
    # Also reranked: the top this many of each ranking on its own, so a
    # chunk only one of them finds can still be read by the reranker.
    # 20, not 10: the answer to "how long does the billing reporter wait
    # before retrying" ranked 17th by its words, and the plans a first
    # install seeds 12th by meaning. Scored with the chat-model ranking,
    # context precision rose from 0.90 to 0.92; without it, it held.
    rerank_per_ranking: int = 20
    rerank_timeout_seconds: float = 5.0
    # Each clause of a question searched on its own as well (service.py):
    # the best `clause_candidates` of each join the reranker's candidates,
    # scored against their clause too. Over the 35 SDK passages that the
    # agent's build requests depend on, the meaning search put 7 in its top
    # 20 searching each request whole, and 24 searching each clause. Each
    # clause costs one embedding, one search and one rerank call.
    clause_search: bool = True
    clause_candidates: int = 5
    max_clauses: int = 6
    # Of each clause's passages, the best this many by the reranker's score
    # against that clause are among the chat model's candidates.
    clause_slots: int = 1
    # Once reranked, a passage scoring below this is not sent, nor is one
    # the reranker never read; the best `rerank_keep` always are. Named
    # capabilities' skill notes are exempt, as for skill_slots.
    rerank_min_score: float = 0.05
    rerank_keep: int = 3

    # /v1/uses: for each value, the service chunks the reranker scores at
    # least `uses_min_score`, at most `uses_top_k`, from the best
    # `uses_candidates` of the hybrid search; without a reranker, the first
    # `uses_top_k`. A fixed three cut the tokenizer's registry, scored 0.995
    # against 0.999 for the first: the relevant ones score alike, so the
    # score decides where they end, not a count.
    uses_top_k: int = 6
    uses_min_score: float = 0.5
    uses_candidates: int = 20

    # rank_skills(): the best `skill_rerank_candidates` by margin get a
    # real cross-encoder comparison against the request, not just their
    # own independent embedding distance -- comfortably more than any
    # genuine request should match, small enough to keep the rerank pass
    # fast. Without a reranker configured, the margin order is final, same
    # as every other reranked path in this service.
    skill_rerank_candidates: int = 6

    # A chat model as the last ranking step (llm_reranker.py), over the
    # best `llm_rerank_candidates`. Unset, the reranker's order is final.
    llm_rerank_url: str = ""  # an OpenAI-compatible /v1/chat/completions
    llm_rerank_model: str = ""
    llm_rerank_candidates: int = 15
    llm_rerank_timeout_seconds: float = 15.0
    llm_rerank_subject: str = "the Multistack platform"

    # An identical search within this many seconds gets the same result,
    # from memory. Without it, two identical searches returned different
    # passages for 11 of the 41 grounding questions -- at the chat model's
    # temperature 1.0 and at 0 alike. 0 turns it off.
    search_cache_seconds: float = 600.0
    search_cache_size: int = 256
    # Send only the passages the model called useful, at least
    # llm_rerank_min_passages of them, rather than filling top_k.
    llm_rerank_only_useful: bool = False
    llm_rerank_min_passages: int = 3
    rerank_query_template: str = "{query}"
    rerank_document_template: str = "{document}"
    # A question is about the platform only if the best chunk is at least
    # this similar and it shares at least this many words with the indexed
    # text. Similarity alone lets greetings through: "hi" sits close to
    # the generic package overviews.
    min_score: float = 0.64
    min_query_terms: int = 2
