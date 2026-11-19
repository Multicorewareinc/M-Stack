"""The search: how two rankings merge, what the gate keeps out, and what
reaches the caller."""

from __future__ import annotations

import prompt
import pytest
from chunking import estimate_tokens
from corpus import Chunk
from indexer import build_index
from service import NotIndexed, Searcher, clauses

from settings import Settings

DIM = 8


def one_hot(i: int) -> list[float]:
    return [1.0 if j == i else 0.0 for j in range(DIM)]


def chunk(n: int, text: str, *, capability: str = "gateway", kind: str = "field") -> Chunk:
    return Chunk(
        id=f"f{n}.py:{n}:{kind}",
        text=text,
        context=f"context {n}",
        capability=capability,
        source_path=f"src/f{n}.py",
        start_line=n,
        end_line=n + 1,
        kind=kind,
    )


class FixedEmbedder:
    """Each chunk gets a chosen vector; every question points along `axis`."""

    def __init__(self, vectors: dict[str, list[float]], axis: int = 0) -> None:
        self.vectors = vectors
        self.axis = axis
        self.asked: list[str] = []

    def embed_documents(self, chunks, *, on_batch=None):
        return [self.vectors[c.text] for c in chunks]

    def embed_query(self, question: str) -> list[float]:
        self.asked.append(question)
        return one_hot(self.axis)


def searcher(store, pairs, *, axis: int = 0, reranker=None, **settings) -> Searcher:
    """`pairs` is [(chunk, vector)]. Gate off unless a test sets it."""
    embedder = FixedEmbedder({c.text: v for c, v in pairs}, axis)
    build_index([c for c, _ in pairs], store, embedder, model="m", dimension=DIM)
    config = {"min_score": 0.0, "min_query_terms": 0, "stub_penalty": 1.0, **settings}
    return Searcher(store, embedder, Settings(**config), reranker)


# ---- merging


def test_a_chunk_found_by_both_rankings_beats_one_found_by_only_one(memory_store):
    s = searcher(
        memory_store,
        [
            (chunk(0, "alpha only dense"), one_hot(3)),
            (chunk(1, "the needle appears here"), one_hot(4)),
            (chunk(2, "needle needle needle here"), one_hot(0)),
        ],
    )
    result = s.search("needle")
    assert result.passages[0].text == "needle needle needle here"
    assert result.passages[0].dense_rank == 1 and result.passages[0].keyword_rank == 1


def test_an_exact_word_match_reaches_the_results_though_meaning_ranks_it_last(memory_store):
    pairs = [(chunk(i, f"unrelated filler {i}"), one_hot(0)) for i in range(6)]
    pairs.append((chunk(6, "cache_auth_url is a credential"), one_hot(7)))
    result = searcher(memory_store, pairs, top_k=3).search("cache_auth_url")
    assert "cache_auth_url is a credential" in [p.text for p in result.passages]


def test_a_capability_boost_reorders_but_never_hides(memory_store):
    pairs = [
        (chunk(0, "same words", capability="billing"), one_hot(0)),
        (chunk(1, "same words too", capability="policy"), one_hot(0)),
    ]
    plain = searcher(memory_store, pairs).search("same words", capabilities=["policy"])
    boosted = searcher(memory_store, pairs, capability_boost=3.0).search(
        "same words", capabilities=["policy"]
    )
    assert plain.passages[0].capability == "billing"
    assert boosted.passages[0].capability == "policy"
    assert {p.capability for p in boosted.passages} == {"billing", "policy"}


def test_class_stubs_can_be_pushed_down(memory_store):
    pairs = [
        (chunk(0, "class X: docstring answer", kind="class"), one_hot(0)),
        (chunk(1, "the real answer text"), one_hot(0)),
    ]
    assert (
        searcher(memory_store, pairs).search("class docstring answer").passages[0].kind == "class"
    )
    assert (
        searcher(memory_store, pairs, stub_penalty=0.1)
        .search("class docstring answer")
        .passages[0]
        .kind
        == "field"
    )


# ---- what reaches the caller


def test_identical_text_from_two_files_is_returned_once(memory_store):
    pairs = [
        (chunk(0, "shared file text"), one_hot(0)),
        (chunk(1, "shared file text"), one_hot(0)),
        (chunk(2, "other"), one_hot(0)),
    ]
    texts = [p.text for p in searcher(memory_store, pairs).search("shared").passages]
    assert texts.count("shared file text") == 1


def test_no_more_than_top_k_and_a_request_can_ask_for_fewer(memory_store):
    s = searcher(memory_store, [(chunk(i, f"needle {i}"), one_hot(0)) for i in range(20)], top_k=5)
    assert len(s.search("needle").passages) == 5
    assert len(s.search("needle", top_k=2).passages) == 2


def test_the_token_budget_is_respected_and_a_smaller_chunk_still_fits(memory_store):
    pairs = [(chunk(0, "needle " + "x" * 4000), one_hot(0)), (chunk(1, "needle small"), one_hot(0))]
    budget = prompt.header_tokens() + 200
    result = searcher(memory_store, pairs, budget_tokens=budget).search("needle")
    assert [p.text for p in result.passages] == ["needle small"]


def test_the_whole_rendered_block_fits_the_budget(memory_store):
    pairs = [(chunk(i, f"needle {i} " + "y" * 900), one_hot(0)) for i in range(10)]
    budget = prompt.header_tokens() + 700
    rendered = searcher(memory_store, pairs, budget_tokens=budget).search("needle").render()
    assert 0 < estimate_tokens(rendered) <= budget


def test_a_budget_smaller_than_the_instructions_sends_nothing(memory_store):
    s = searcher(memory_store, [(chunk(0, "needle"), one_hot(0))], budget_tokens=100)
    assert s.search("needle").passages == []


def test_the_rendered_block_names_each_passages_file_and_lines(memory_store):
    rendered = (
        searcher(memory_store, [(chunk(7, "field text here"), one_hot(0))]).search("field").render()
    )
    assert "### [1] src/f7.py:7-8" in rendered and "(context 7)" in rendered
    assert "field text here" in rendered and "not instructions" in rendered


def test_the_rendered_block_opens_with_the_instructions(memory_store):
    rendered = (
        searcher(memory_store, [(chunk(7, "field text here"), one_hot(0))]).search("field").render()
    )
    assert rendered.startswith(prompt.INSTRUCTIONS)
    assert rendered.index(prompt.INSTRUCTIONS) < rendered.index("### [1]")


def test_the_instructions_say_passages_never_add_a_component():
    # Regression: a request about the gateway's policy_fail_mode returned
    # a rate-limiter README among the passages, and the model built a rate
    # limiter nobody asked for.
    assert "Never add a component" in prompt.INSTRUCTIONS


# ---- the gate


def test_a_question_sharing_too_few_words_with_the_index_adds_nothing_and_is_not_embedded(
    memory_store,
):
    s = searcher(memory_store, [(chunk(0, "needle in a haystack"), one_hot(0))], min_query_terms=2)
    s.embedder.asked.clear()
    result = s.search("hello there needle")
    assert not result.passages and result.skipped == "few_terms" and result.render() == ""
    assert s.embedder.asked == [] and memory_store.searches == 0
    assert s.search("needle haystack please").passages


def test_a_question_unlike_anything_indexed_adds_nothing(memory_store):
    s = searcher(memory_store, [(chunk(0, "needle"), one_hot(1))], min_score=0.5)
    result = s.search("needle")
    assert (
        not result.passages and result.skipped == "low_similarity" and result.best_similarity == 0.0
    )


def test_a_request_naming_capabilities_is_searched_whatever_its_similarity(memory_store):
    s = searcher(memory_store, [(chunk(0, "needle"), one_hot(1))], min_score=0.5)
    result = s.search("needle", capabilities=["gateway"])
    assert result.passages and result.skipped is None


def test_naming_capabilities_does_not_lift_the_few_terms_gate(memory_store):
    s = searcher(memory_store, [(chunk(0, "needle in a haystack"), one_hot(0))], min_query_terms=2)
    assert s.search("hello there", capabilities=["gateway"]).skipped == "few_terms"


def test_an_empty_index_is_reported_not_searched(memory_store):
    s = Searcher(memory_store, FixedEmbedder({}), Settings())
    with pytest.raises(NotIndexed):
        s.search("anything")


# ---- slots held for the named capabilities' own notes


def _code_and_a_rule(n_code: int = 8):
    """`n_code` code chunks that match "limiter fail" on both rankings,
    then a policy rule that matches it on words alone."""
    pairs = [
        (chunk(i, f"limiter fail code {i}", capability="policy"), one_hot(0)) for i in range(n_code)
    ]
    pairs.append(
        (
            chunk(50, "limiter fail rule: set cache_auth_url", capability="policy", kind="rule"),
            one_hot(7),
        )
    )
    return pairs


def test_a_rule_of_a_named_capability_gets_a_slot_code_would_otherwise_fill(memory_store):
    s = searcher(memory_store, _code_and_a_rule(), top_k=4, skill_slots=2)
    held = [p.text for p in s.search("limiter fail", capabilities=["policy"]).passages]
    plain = [p.text for p in s.search("limiter fail").passages]
    assert "limiter fail rule: set cache_auth_url" in held and len(held) == 4
    assert "limiter fail rule: set cache_auth_url" not in plain


def test_held_passages_keep_their_rank_order(memory_store):
    s = searcher(memory_store, _code_and_a_rule(), top_k=4, skill_slots=2)
    result = s.search("limiter fail", capabilities=["policy"])
    assert result.passages[-1].kind == "rule"
    assert [p.score for p in result.passages] == sorted(
        (p.score for p in result.passages), reverse=True
    )


def test_no_slot_for_notes_of_a_capability_nobody_named(memory_store):
    s = searcher(memory_store, _code_and_a_rule(), top_k=4, skill_slots=2)
    texts = [p.text for p in s.search("limiter fail", capabilities=["billing"]).passages]
    assert "limiter fail rule: set cache_auth_url" not in texts


def test_zero_skill_slots_turns_it_off(memory_store):
    s = searcher(memory_store, _code_and_a_rule(), top_k=4, skill_slots=0)
    texts = [p.text for p in s.search("limiter fail", capabilities=["policy"]).passages]
    assert "limiter fail rule: set cache_auth_url" not in texts


def test_no_more_than_skill_slots_are_held(memory_store):
    pairs = [
        (chunk(i, f"limiter fail code {i}", capability="policy"), one_hot(0)) for i in range(8)
    ]
    pairs += [
        (chunk(50 + i, f"limiter fail rule {i}", capability="policy", kind="rule"), one_hot(7))
        for i in range(5)
    ]
    s = searcher(memory_store, pairs, top_k=4, skill_slots=2)
    kinds = [p.kind for p in s.search("limiter fail", capabilities=["policy"]).passages]
    assert kinds.count("rule") == 2 and len(kinds) == 4


# ---- a caller that already has the notes


def test_notes_loaded_leaves_out_the_named_capabilities_notes(memory_store):
    s = searcher(memory_store, _code_and_a_rule(), top_k=4, skill_slots=2)
    result = s.search("limiter fail", capabilities=["policy"], notes_loaded=True)
    assert [p.kind for p in result.passages if p.kind == "rule"] == []
    assert len(result.passages) == 4


def test_notes_loaded_keeps_notes_of_a_capability_nobody_named(memory_store):
    pairs = [
        (chunk(i, f"limiter fail code {i}", capability="policy"), one_hot(0)) for i in range(2)
    ]
    pairs.append(
        (chunk(50, "limiter fail rule for billing", capability="billing", kind="rule"), one_hot(0))
    )
    s = searcher(memory_store, pairs, top_k=4)
    texts = [
        p.text
        for p in s.search("limiter fail", capabilities=["policy"], notes_loaded=True).passages
    ]
    assert "limiter fail rule for billing" in texts


def test_notes_loaded_is_part_of_the_cache_key(memory_store):
    s = searcher(memory_store, _code_and_a_rule(), top_k=4, skill_slots=2, search_cache_seconds=600)
    with_notes = s.search("limiter fail", capabilities=["policy"])
    without = s.search("limiter fail", capabilities=["policy"], notes_loaded=True)
    assert any(p.kind == "rule" for p in with_notes.passages)
    assert not any(p.kind == "rule" for p in without.passages)


# ---- reranking


class ScoringReranker:
    """Scores a document by the number in its text after "score=", so a
    test decides the reranker's order outright."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def rerank(self, query, documents):
        self.calls.append((query, list(documents)))
        return [float(d.split("score=")[1].split()[0]) for d in documents]


class BrokenReranker:
    def rerank(self, query, documents):
        from reranker import RerankError

        raise RerankError("reranker unreachable")


def _ranked(n: int):
    """n chunks whose hybrid order is 0, 1, 2 ... and whose reranker
    score rises with their number, so reranking reverses them."""
    return [
        (chunk(i, f"needle score={i} x{i}"), [1.0 - i / 100] + [0.0] * (DIM - 1)) for i in range(n)
    ]


def test_the_reranker_reorders_the_top_of_the_hybrid_ranking(memory_store):
    s = searcher(
        memory_store, _ranked(5), reranker=ScoringReranker(), top_k=5, rerank_min_score=0.0
    )
    result = s.search("needle")
    assert result.reranked
    assert [p.text.split()[1] for p in result.passages] == [f"score={i}" for i in (4, 3, 2, 1, 0)]
    assert [p.rerank_score for p in result.passages] == [4.0, 3.0, 2.0, 1.0, 0.0]


def test_only_the_top_candidates_are_sent_and_the_rest_follow_in_hybrid_order(memory_store):
    reranker = ScoringReranker()
    s = searcher(
        memory_store,
        _ranked(6),
        reranker=reranker,
        top_k=6,
        rerank_candidates=3,
        rerank_per_ranking=0,
        rerank_min_score=0.0,
        rerank_keep=6,
    )
    passages = s.search("needle").passages
    ((_query, sent),) = reranker.calls
    assert len(sent) == 3
    assert [p.text.split()[1] for p in passages] == [
        "score=2",
        "score=1",
        "score=0",
        "score=3",
        "score=4",
        "score=5",
    ]
    assert passages[3].rerank_score is None


def _scored(scores, *, rules=()):
    """Chunks the reranker scores `scores` in hybrid order, then skill notes
    of "policy" it scores 0."""
    pairs = [
        (chunk(i, f"needle score={s} x{i}"), [1.0 - i / 100] + [0.0] * (DIM - 1))
        for i, s in enumerate(scores)
    ]
    pairs += [
        (
            chunk(50 + i, f"needle score=0 rule{i}", capability="policy", kind="rule"),
            [0.5] + [0.0] * (DIM - 1),
        )
        for i in range(len(rules))
    ]
    return pairs


def test_a_passage_the_reranker_scores_irrelevant_is_not_sent(memory_store):
    s = searcher(
        memory_store,
        _scored([0.9, 0.8, 0.7, 0.6, 0.01, 0.02]),
        reranker=ScoringReranker(),
        top_k=6,
        rerank_min_score=0.05,
        rerank_keep=3,
    )
    assert [p.rerank_score for p in s.search("needle").passages] == [0.9, 0.8, 0.7, 0.6]


def test_the_best_few_are_sent_whatever_they_scored(memory_store):
    s = searcher(
        memory_store,
        _scored([0.04, 0.03, 0.02, 0.01]),
        reranker=ScoringReranker(),
        top_k=4,
        rerank_min_score=0.05,
        rerank_keep=3,
    )
    assert [p.rerank_score for p in s.search("needle").passages] == [0.04, 0.03, 0.02]


def test_a_passage_the_reranker_never_read_is_not_sent(memory_store):
    s = searcher(
        memory_store,
        _scored([0.9, 0.8, 0.7, 0.6, 0.5]),
        reranker=ScoringReranker(),
        top_k=5,
        rerank_candidates=3,
        rerank_per_ranking=0,
        rerank_min_score=0.05,
        rerank_keep=1,
    )
    assert [p.rerank_score for p in s.search("needle").passages] == [0.9, 0.8, 0.7]


def test_a_named_capabilitys_skill_note_is_sent_whatever_it_scored(memory_store):
    s = searcher(
        memory_store,
        _scored([0.9, 0.8, 0.7], rules=["r"]),
        reranker=ScoringReranker(),
        top_k=5,
        rerank_min_score=0.05,
        rerank_keep=1,
    )
    named = s.search("needle", capabilities=["policy"]).passages
    unnamed = s.search("needle").passages
    assert [p.kind for p in named].count("rule") == 1
    assert [p.kind for p in unnamed].count("rule") == 0


def test_without_a_reranker_nothing_is_dropped(memory_store):
    s = searcher(memory_store, _scored([0.01, 0.01, 0.01, 0.01]), top_k=4, rerank_min_score=0.05)
    assert len(s.search("needle").passages) == 4


def test_the_reranker_sees_each_chunks_context_and_text(memory_store):
    reranker = ScoringReranker()
    searcher(memory_store, _ranked(1), reranker=reranker).search("needle")
    ((_query, sent),) = reranker.calls
    assert sent == ["context 0\n\nneedle score=0 x0"]


def test_a_failing_reranker_keeps_the_hybrid_order(memory_store):
    plain = searcher(memory_store, _ranked(4), top_k=4).search("needle")
    fallback = searcher(memory_store, _ranked(4), reranker=BrokenReranker(), top_k=4).search(
        "needle"
    )
    assert not fallback.reranked
    assert [p.text for p in fallback.passages] == [p.text for p in plain.passages]


def test_a_gated_question_never_reaches_the_reranker(memory_store):
    reranker = ScoringReranker()
    s = searcher(memory_store, _ranked(2), reranker=reranker, min_query_terms=5)
    assert s.search("needle").skipped == "few_terms" and reranker.calls == []


def test_zero_candidates_turns_reranking_off(memory_store):
    reranker = ScoringReranker()
    result = searcher(memory_store, _ranked(3), reranker=reranker, rerank_candidates=0).search(
        "needle"
    )
    assert not result.reranked and reranker.calls == []


def test_skill_slots_still_apply_after_reranking(memory_store):
    pairs = _ranked(8)
    pairs.append(
        (chunk(50, "needle score=-1 rule text", capability="policy", kind="rule"), one_hot(7))
    )
    s = searcher(memory_store, pairs, reranker=ScoringReranker(), top_k=4, skill_slots=1)
    kinds = [p.kind for p in s.search("needle", capabilities=["policy"]).passages]
    assert "rule" in kinds and len(kinds) == 4


def test_a_close_meaning_match_the_word_search_misses_is_still_reranked(memory_store):
    """Chunks both rankings find outscore one only the meaning search finds
    in the merge, which can push it below the reranker's cut; the top of
    each ranking is reranked too, so it can still come first."""
    pairs = [(chunk(i, f"needle filler score=0 n{i}"), [0.5] + [0.0] * (DIM - 1)) for i in range(6)]
    # Nearest by meaning, but without the word "needle": the word search misses it.
    pairs.append((chunk(9, "the answer score=9"), [1.0] + [0.0] * (DIM - 1)))
    reranker = ScoringReranker()
    capped = searcher(
        memory_store, pairs, reranker=reranker, top_k=3, rerank_candidates=2, rerank_per_ranking=0
    ).search("needle")
    widened = searcher(
        memory_store,
        pairs,
        reranker=ScoringReranker(),
        top_k=3,
        rerank_candidates=2,
        rerank_per_ranking=1,
    ).search("needle")
    assert "the answer score=9" not in [p.text for p in capped.passages]
    assert widened.passages[0].text == "the answer score=9"


# ---- searching each clause on its own

REQUEST = (
    "Add a gateway for our models, and let requests through when the limiter is down. "
    "Keys are in the Secret gw-keys."
)
CLAUSE = "let requests through when the limiter is down"
TARGET = "fail mode open admits callers"


class ClauseEmbedder(FixedEmbedder):
    """Every question points along axis 0, except those given their own."""

    def __init__(self, vectors, axes: dict[str, int], broken: tuple[str, ...] = ()) -> None:
        super().__init__(vectors)
        self.axes = axes
        self.broken = broken

    def embed_query(self, question: str) -> list[float]:
        self.asked.append(question)
        if question in self.broken:
            from embeddings import EmbeddingError

            raise EmbeddingError("embedding server unreachable")
        return one_hot(self.axes.get(question, 0))


class TableReranker:
    """Scores each (query, chunk text) from a table, 0 when not listed."""

    def __init__(self, table: dict[tuple[str, str], float]) -> None:
        self.table = table
        self.calls: list[tuple[str, list[str]]] = []

    def rerank(self, query, documents):
        self.calls.append((query, list(documents)))
        return [self.table.get((query, d.split("\n\n", 1)[1]), 0.0) for d in documents]


class RecordingLLM:
    """A chat-model ranking that records what it was shown and calls nothing useful."""

    def __init__(self) -> None:
        self.seen: list[list[str]] = []

    def useful(self, question, candidates):
        self.seen.append([p.text for p in candidates])
        return []


def _clause_searcher(store, *, reranker=None, llm_reranker=None, broken=(), **settings) -> Searcher:
    """Six chunks near the request as a whole, and the one its second
    clause is about, nearest that clause alone. A pool of 3 keeps it out
    of the whole request's own search."""
    pairs = [
        (chunk(i, f"gateway overview part{i}"), [1.0 - i / 100] + [0.0] * (DIM - 1))
        for i in range(6)
    ]
    pairs.append((chunk(9, TARGET), one_hot(1)))
    embedder = ClauseEmbedder({c.text: v for c, v in pairs}, {CLAUSE: 1}, broken)
    build_index([c for c, _ in pairs], store, embedder, model="m", dimension=DIM)
    config = {"min_score": 0.0, "min_query_terms": 0, "stub_penalty": 1.0, "pool": 3, **settings}
    return Searcher(store, embedder, Settings(**config), reranker, llm_reranker)


def test_a_request_is_split_into_sentences_and_their_clauses():
    assert clauses(REQUEST, 6) == [
        "Add a gateway for our models",
        CLAUSE,
        "Keys are in the Secret gw-keys",
    ]
    assert clauses(REQUEST, 1) == ["Add a gateway for our models"]
    # Parts under three words, and a question that is one clause, add nothing.
    assert clauses("Do it. Now please.", 6) == []
    assert clauses("what port does the tokenizer use", 6) == []


def test_a_passage_only_a_clause_finds_is_returned(memory_store):
    found = _clause_searcher(memory_store, top_k=8).search(REQUEST)
    assert TARGET in [p.text for p in found.passages]


def test_without_clause_search_only_the_whole_request_is_searched(memory_store):
    s = _clause_searcher(memory_store, top_k=8, clause_search=False)
    found = s.search(REQUEST)
    assert TARGET not in [p.text for p in found.passages]
    assert s.embedder.asked[-1:] == [REQUEST]


def test_a_clause_found_passage_is_scored_against_its_clause(memory_store):
    reranker = TableReranker({(CLAUSE, TARGET): 0.9})
    found = _clause_searcher(memory_store, reranker=reranker, top_k=4).search(REQUEST)
    assert found.passages[0].text == TARGET
    assert found.passages[0].rerank_score == 0.9
    assert any(
        query == CLAUSE and f"context 9\n\n{TARGET}" in sent for query, sent in reranker.calls
    )


def test_each_clauses_best_passage_reaches_the_chat_model(memory_store):
    # Against the request as a whole the overview outscores it; against its
    # own clause it is the best there is.
    table = {(REQUEST, f"gateway overview part{i}"): 0.9 for i in range(6)}
    table[(CLAUSE, TARGET)] = 0.5
    settings = {"llm_rerank_candidates": 3, "skill_slots": 0}

    held = RecordingLLM()
    _clause_searcher(
        memory_store, reranker=TableReranker(table), llm_reranker=held, **settings
    ).search(REQUEST)
    assert TARGET in held.seen[0]

    unheld = RecordingLLM()
    _clause_searcher(
        memory_store, reranker=TableReranker(table), llm_reranker=unheld, clause_slots=0, **settings
    ).search(REQUEST)
    assert TARGET not in unheld.seen[0]


def test_a_clause_that_fails_to_embed_is_skipped(memory_store):
    s = _clause_searcher(memory_store, top_k=8, broken=(CLAUSE,))
    found = s.search(REQUEST)
    assert TARGET not in [p.text for p in found.passages]
    assert len(found.passages) > 0


def test_max_clauses_caps_the_extra_searches(memory_store):
    s = _clause_searcher(memory_store, max_clauses=1)
    s.embedder.asked.clear()
    s.search(REQUEST)
    assert s.embedder.asked == [REQUEST, "Add a gateway for our models"]


# ---- the search cache


def test_an_identical_search_is_answered_from_the_cache(memory_store):
    s = searcher(memory_store, [(chunk(0, "needle"), one_hot(0))])
    s.embedder.asked.clear()
    first = s.search("needle", capabilities=["gateway"])
    second = s.search("needle", capabilities=["gateway"])
    assert second is first and s.embedder.asked == ["needle"]


def test_a_different_question_or_capability_is_searched_again(memory_store):
    s = searcher(memory_store, [(chunk(0, "needle"), one_hot(0))])
    s.embedder.asked.clear()
    s.search("needle", capabilities=["gateway"])
    s.search("needle", capabilities=["billing"])
    s.search("needle haystack", capabilities=["gateway"])
    assert len(s.embedder.asked) == 3


def test_a_cached_result_expires(memory_store, monkeypatch):
    import service

    s = searcher(memory_store, [(chunk(0, "needle"), one_hot(0))], search_cache_seconds=60)
    clock = [1000.0]
    monkeypatch.setattr(service.time, "monotonic", lambda: clock[0])
    s.embedder.asked.clear()
    s.search("needle")
    clock[0] += 61
    s.search("needle")
    assert len(s.embedder.asked) == 2


def test_a_rebuilt_index_is_not_answered_from_the_old_cache(memory_store):
    s = searcher(memory_store, [(chunk(0, "needle"), one_hot(0))])
    s.search("needle")
    s.embedder.vectors["needle two"] = one_hot(0)
    build_index([chunk(1, "needle two")], memory_store, s.embedder, model="m", dimension=DIM)
    s.load()
    s.embedder.asked.clear()
    s.search("needle")
    assert s.embedder.asked == ["needle"]


def test_the_cache_can_be_turned_off(memory_store):
    s = searcher(memory_store, [(chunk(0, "needle"), one_hot(0))], search_cache_seconds=0)
    s.embedder.asked.clear()
    s.search("needle")
    s.search("needle")
    assert len(s.embedder.asked) == 2
