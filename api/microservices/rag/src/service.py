"""Finds the chunks that answer a question, and formats them for a prompt.

Two rankings are made independently, by meaning (embedding similarity) and
by exact words (BM25). Each is good where the other is blind, so they are
merged by rank: a chunk scores 1/(rrf_k + rank) in each list it appears
in, and the scores add. Merging on rank rather than on score is what lets
two scales that can't be compared, cosine and BM25, be combined without
tuning weights against each other.

A build request is mostly the user's own values -- names, Secrets,
addresses -- around the one clause that says what to change, and searched
whole, that clause is drowned out. So each clause is searched on its own
too: the best few it finds join the reranker's candidates, scored against
that clause as well as the whole request.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field, replace
from typing import Protocol

import prompt
from chunking import estimate_tokens
from corpus import SKILL_KINDS
from embeddings import EmbeddingError
from llm_reranker import LLMReranker, LLMRerankError
from reranker import Reranker, RerankError
from sparse import query_vector, term_id, tokenize
from store import Hit, QdrantStore

from settings import Settings

logger = logging.getLogger(__name__)

META_REFRESH_SECONDS = 60


class NotIndexed(RuntimeError):
    """Nothing has been indexed yet: the vector database has no metadata."""


class _QueryEmbedder(Protocol):
    def embed_query(self, question: str) -> list[float]: ...
    def embed_queries(self, questions: Sequence[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class Passage:
    text: str
    context: str
    path: str
    start: int
    end: int
    kind: str
    capability: str | None
    score: float
    dense_rank: int | None = None
    keyword_rank: int | None = None
    rerank_score: float | None = None
    symbol: str | None = None

    def source(self) -> dict:
        return {
            "path": self.path,
            "start": self.start,
            "end": self.end,
            "context": self.context,
            "kind": self.kind,
            "capability": self.capability,
            "symbol": self.symbol,
            "score": round(self.score, 4),
            "rerank_score": None if self.rerank_score is None else round(self.rerank_score, 4),
        }


@dataclass(frozen=True)
class Result:
    question: str
    passages: list[Passage] = field(default_factory=list)
    best_similarity: float = 0.0
    # Why nothing was returned, when nothing was: "few_terms" or "low_similarity".
    skipped: str | None = None
    # Whether the reranker ordered these; False when none is configured or
    # it failed and the hybrid order was kept.
    reranked: bool = False
    # Whether the chat model chose the order; False when none is configured
    # or it failed and the reranker's order was kept.
    llm_reranked: bool = False

    def render(self) -> str:
        return prompt.render(self.passages)


@dataclass(frozen=True)
class Use:
    """The service code found working with one value of one field."""

    capability: str
    name: str
    value: str
    passages: list[Passage] = field(default_factory=list)


# Sentence ends, semicolons, and ", and" / ", with" / ", but" between clauses.
_CLAUSE = re.compile(r"(?<=[.!?;])\s+|,\s+(?:and|with|but)\s+|;\s*")


def clauses(question: str, limit: int) -> list[str]:
    """The parts of `question` searched on their own, at most `limit`: its
    sentences, split again between clauses. A part under three words says
    too little to search by; one that is the whole question already is."""
    whole = question.strip(" .,;!?")
    parts: list[str] = []
    for part in _CLAUSE.split(question):
        part = part.strip(" .,;!?")
        if len(part.split()) >= 3 and part != whole and part not in parts:
            parts.append(part)
    return parts[:limit]


def _key(p: Passage) -> tuple[str, int, int]:
    return (p.path, p.start, p.end)


def _passage(
    hit: Hit, *, score: float, dense_rank: int | None, keyword_rank: int | None
) -> Passage:
    p = hit.payload
    return Passage(
        text=p["text"],
        context=p["context"],
        path=p["source_path"],
        start=p["start_line"],
        end=p["end_line"],
        kind=p["kind"],
        capability=p.get("capability") or None,
        score=score,
        dense_rank=dense_rank,
        keyword_rank=keyword_rank,
        symbol=p.get("symbol") or None,
    )


class Searcher:
    def __init__(
        self,
        store: QdrantStore,
        embedder: _QueryEmbedder,
        settings: Settings,
        reranker: Reranker | None = None,
        llm_reranker: LLMReranker | None = None,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.settings = settings
        self.reranker = reranker
        self.llm_reranker = llm_reranker
        self._cache: OrderedDict[tuple, tuple[float, Result]] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._terms: set[int] | None = None
        self._meta: dict | None = None
        self._loaded_at = 0.0

    # ---- what was indexed

    def load(self) -> dict | None:
        """Reads the index metadata: what it was built from and which words
        occur in it. None if nothing has been indexed."""
        meta = self.store.read_meta()
        self._meta = meta
        self._terms = set(meta["terms"]) if meta else None
        self._loaded_at = time.monotonic()
        return meta

    def meta(self) -> dict | None:
        if self._terms is None or time.monotonic() - self._loaded_at > META_REFRESH_SECONDS:
            self.load()
        return self._meta

    # ---- searching

    def search(
        self,
        question: str,
        *,
        capabilities: Collection[str] = (),
        top_k: int | None = None,
        budget_tokens: int | None = None,
        notes_loaded: bool = False,
    ) -> Result:
        """The passages for `question`. An identical search within
        SEARCH_CACHE_SECONDS gets the same result: the chat-model ranking
        takes seconds, and a caller that searches, then asks the agent --
        which searches again -- must see the passages the agent saw.

        `notes_loaded`: the caller already has the named capabilities'
        skill notes, so none of them are returned."""
        s = self.settings
        meta = self.meta()
        if meta is None:
            raise NotIndexed("nothing has been indexed yet")
        if s.search_cache_seconds <= 0:
            return self._search(question, capabilities, top_k, budget_tokens, notes_loaded)
        # The collection in the key: a rebuilt index is a new collection.
        key = (
            meta.get("collection"),
            question,
            tuple(sorted(capabilities)),
            top_k,
            budget_tokens,
            notes_loaded,
        )
        now = time.monotonic()
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < s.search_cache_seconds:
                self._cache.move_to_end(key)
                return hit[1]
        result = self._search(question, capabilities, top_k, budget_tokens, notes_loaded)
        with self._cache_lock:
            self._cache[key] = (now, result)
            self._cache.move_to_end(key)
            while len(self._cache) > s.search_cache_size:
                self._cache.popitem(last=False)
        return result

    def _search(
        self,
        question: str,
        capabilities: Collection[str],
        top_k: int | None,
        budget_tokens: int | None,
        notes_loaded: bool = False,
    ) -> Result:
        s = self.settings

        # Checked before embedding: a greeting costs no embedding call.
        terms = self._terms or set()
        known = sum(1 for t in set(tokenize(question)) if term_id(t) in terms)
        if known < s.min_query_terms:
            return Result(question, skipped="few_terms")

        dense_hits, keyword_hits = self.store.search(
            self.embedder.embed_query(question), query_vector(question), s.pool
        )
        best = dense_hits[0].score if dense_hits else 0.0
        wanted = set(capabilities)
        # A caller that names capabilities has already judged the request to
        # be about them. A build request carries addresses and names, which
        # pull its similarity down to the threshold: 0.63 to 0.69 measured.
        if best < s.min_score and not wanted:
            return Result(question, best_similarity=best, skipped="low_similarity")

        ranked = self._merge(dense_hits, keyword_hits, wanted, notes_loaded)
        found_by = self._search_clauses(question, ranked, wanted, notes_loaded)
        ranked, reranked, clause_best = self._rerank(question, ranked, found_by)
        if reranked:
            ranked = self._drop_irrelevant(ranked, wanted)
        # The best-ranked notes of the capabilities the caller named. A rule
        # that matches on one ranking loses to code matching on both, and is
        # often the part of the answer the code alone doesn't give.
        held = [
            i for i, p in enumerate(ranked) if p.kind in SKILL_KINDS and p.capability in wanted
        ][: s.skill_slots]
        # Each clause's best passage is among the chat model's candidates.
        # Ranked against the whole request, the field a clause asks about
        # came 16th to 31st for 9 of 35 build requests -- out of the 15 the
        # model reads -- and every one that reached the model was kept.
        held += [i for i, p in enumerate(ranked) if _key(p) in clause_best and i not in held]
        # The budget is for the whole block the caller adds to its prompt,
        # instructions and examples included.
        room = (budget_tokens or s.budget_tokens) - prompt.header_tokens()
        chosen = self._llm_rerank(
            question, self._pack(ranked, s.llm_rerank_candidates, 10**9, held)
        )
        if chosen is None:
            packed = self._pack(ranked, top_k or s.top_k, room, held)
        else:
            ordered, useful = chosen
            if s.llm_rerank_only_useful:
                # Only what the model called useful, topped up to a floor so
                # a question it judged wrongly still gets passages.
                ordered = ordered[: max(useful, s.llm_rerank_min_passages)]
            packed = self._pack(ordered, top_k or s.top_k, room)
        return Result(question, packed, best, reranked=reranked, llm_reranked=chosen is not None)

    # ---- exact lookups

    def fields(
        self,
        capabilities: Sequence[str],
        *,
        names: Collection[str] = (),
        budget_tokens: int | None = None,
    ) -> Result:
        """The SDK field chunks of `capabilities` -- each field a plan can
        set, with its comment, default and limits -- read by label, with no
        embedding call. `names` narrows them to those fields. Ordered as
        the capabilities are given, then as in the source, and cut at the
        budget, so the first capability, the one the request named, comes
        whole. Constants (CAPABILITY, FROM_STACK, ...) are not fields a
        plan sets, and are left out."""
        if self.meta() is None:
            raise NotIndexed("nothing has been indexed yet")
        if not capabilities:
            return Result("")
        conditions: dict[str, str | Sequence[str]] = {
            "origin": "sdk",
            "kind": "field",
            "capability": list(capabilities),
        }
        if names:
            conditions["field_name"] = list(names)
        order = {c: i for i, c in enumerate(capabilities)}
        hits = [
            h
            for h in self.store.matching(conditions)
            if h.payload.get("field_name") and not h.payload["field_name"].isupper()
        ]
        hits.sort(
            key=lambda h: (
                order.get(h.payload["capability"], len(order)),
                h.payload["source_path"],
                h.payload["start_line"],
            )
        )
        budget = budget_tokens or self.settings.budget_tokens
        chosen: list[Passage] = []
        for hit in hits:
            p = _passage(hit, score=0.0, dense_rank=None, keyword_rank=None)
            if estimate_tokens(prompt.render_fields([*chosen, p])) > budget:
                break
            chosen.append(p)
        return Result("", chosen)

    def dependencies(self, capabilities: Sequence[str]) -> Result:
        """Every direct dependency edge for `capabilities` -- one passage
        per (component, dependency) pair, read by exact tag
        (kind="dependency"), no embedding call, same reasoning as
        fields(). Not origin="sdk": these are synthetic chunks built from
        mcp_tools/architecture.py's DEPENDS_ON/REASON, not cut from the
        SDK itself. One hop only -- orchestration/selection.py's chain()
        walks the resulting edges breadth-first for the full transitive
        closure, the same way it already does over the in-process map
        this replaces."""
        if self.meta() is None:
            raise NotIndexed("nothing has been indexed yet")
        if not capabilities:
            return Result("")
        conditions = {"kind": "dependency", "capability": list(capabilities)}
        hits = list(self.store.matching(conditions))
        hits.sort(key=lambda h: (h.payload["capability"], h.payload.get("symbol") or ""))
        chosen = [_passage(h, score=0.0, dense_rank=None, keyword_rank=None) for h in hits]
        return Result("", chosen)

    def tools_for(self, capabilities: Sequence[str]) -> Result:
        """Which MCP tool builds each of `capabilities` on its own, read
        by exact tag (kind="tool_mapping"). Synthetic chunks built from
        orchestration/selection.py's TOOL_FOR_SKILL -- a fixed naming
        convention, not something with any ambiguity for a search to
        resolve."""
        if self.meta() is None:
            raise NotIndexed("nothing has been indexed yet")
        if not capabilities:
            return Result("")
        conditions = {"kind": "tool_mapping", "capability": list(capabilities)}
        hits = list(self.store.matching(conditions))
        chosen = [_passage(h, score=0.0, dense_rank=None, keyword_rank=None) for h in hits]
        return Result("", chosen)

    def rank_skills(self, text: str) -> list[tuple[str, float]]:
        """Every indexed skill, ranked by (how closely its own trigger
        description matches `text`) MINUS (how closely `text` matches
        that skill's own known false-match examples, if it has any),
        then the best `skill_rerank_candidates` of those re-ordered by a
        real cross-encoder comparison against `text` -- not two
        independent vectors compared in isolation, but the (request,
        skill) pair read together, the same joint judgment /v1/search's
        own reranking already makes for passages (see _rerank_top()).
        One embedding call; no reranker configured falls back to the
        margin order alone, same guard every other reranked path in this
        service uses -- the caller (agentic's orchestration/selection.py)
        decides what score counts as relevant and what to do with the rest.

        The subtraction exists because the raw score alone cannot make
        this call: "let requests through if the rate-limit service can't
        be reached" (about the gateway's own policy_fail_mode field, not
        a request for a rate limiter) scored *higher* against policy
        (0.734) than a genuine rate-limiting request (0.694) -- both are
        genuinely about the same topic, so similarity to the skill's own
        description alone cannot separate "about" from "for." A request
        that's actually asking for a skill usually scores far from that
        skill's curated false-match examples; one that's merely about the
        same topic as a false match scores close to it too, which cancels
        most of its raw score out. A skill with no false-match examples
        indexed (see corpus.py's skill_counterexample_chunks()) gets 0
        subtracted -- its margin is just its raw score, same as before
        this existed.

        Not a guarantee for every pair, confirmed directly: the exact
        fail-open sentence above, tested live against the real index,
        still scored higher than a genuine rate-limiting request even
        with itself indexed as policy's own counterexample (0.6174 vs.
        0.5919) -- close enough in meaning that subtracting one score
        from the other narrows the gap without closing it. Deliberately
        not chased further here -- see
        orchestration/selection.py's FALSE_MATCHES comment for why, and
        the human-in-the-loop work for how a case this close is meant to
        be handled instead of by this subtraction alone."""
        if self.meta() is None:
            raise NotIndexed("nothing has been indexed yet")
        query = self.embedder.embed_query(text)
        sparse = {"indices": [], "values": []}
        dense_hits, _ = self.store.search(
            query,
            sparse,
            50,  # comfortably more than the skill count; every one comes back ranked
            conditions={"kind": "skill_intent"},
        )
        counter_hits, _ = self.store.search(
            query, sparse, 50, conditions={"kind": "skill_counterexample"}
        )
        best_counter: dict[str, float] = {}
        for h in counter_hits:
            cap = h.payload["capability"]
            best_counter[cap] = max(best_counter.get(cap, 0.0), h.score)
        margins = [
            (h, h.score - best_counter.get(h.payload["capability"], 0.0)) for h in dense_hits
        ]
        margins.sort(key=lambda pair: -pair[1])
        candidates = [
            _passage(h, score=margin, dense_rank=None, keyword_rank=None) for h, margin in margins
        ]
        reranked = self._rerank_top(text, candidates, self.settings.skill_rerank_candidates)
        return [
            (p.capability, p.rerank_score if p.rerank_score is not None else p.score)
            for p in reranked
        ]

    def uses(self, items: Sequence[tuple[str, str, str]], *, top_k: int | None = None) -> list[Use]:
        """For each (capability, field, value), the service code that works
        with that field. The SDK takes whatever its type allows; the service
        that runs may take less -- the tokenizer registers its encodings by
        name -- and only its code says so. The query is the field's own SDK
        chunk with the value, searched among that capability's service
        chunks only: the chunk names the setting in the words the service
        code uses for it, where the value alone often isn't there at all."""
        s = self.settings
        if self.meta() is None:
            raise NotIndexed("nothing has been indexed yet")
        if not items:
            return []
        queries = []
        for capability, name, value in items:
            own = self.store.matching(
                {"origin": "sdk", "kind": "field", "capability": capability, "field_name": name},
                limit=4,
            )
            about = "\n\n".join(f"{h.payload['context']}\n{h.payload['text']}" for h in own)
            queries.append(f"{about or name}\n\nvalue: {value}")
        vectors = self.embedder.embed_queries(queries)
        found = []
        for (capability, name, value), query, vector in zip(items, queries, vectors, strict=True):
            dense_hits, keyword_hits = self.store.search(
                vector,
                query_vector(query),
                s.pool,
                conditions={"origin": "service", "capability": capability},
            )
            ranked = self._merge(dense_hits, keyword_hits, {capability}, notes_loaded=False)
            ranked = self._rerank_top(query, ranked, s.uses_candidates)
            if ranked and ranked[0].rerank_score is not None:
                ranked = [
                    p
                    for p in ranked
                    if p.rerank_score is not None and p.rerank_score >= s.uses_min_score
                ]
            found.append(Use(capability, name, value, ranked[: top_k or s.uses_top_k]))
        return found

    def _rerank_top(self, query: str, ranked: list[Passage], n: int) -> list[Passage]:
        """The first `n` of `ranked` reordered by the reranker, or `ranked`
        unchanged when there is none or it fails."""
        head = ranked[:n]
        if self.reranker is None or not head:
            return ranked
        try:
            scores = self.reranker.rerank(query, [f"{p.context}\n\n{p.text}" for p in head])
        except RerankError as e:
            logger.warning("rerank skipped, keeping the hybrid order: %s", e)
            return ranked
        order = sorted(range(len(head)), key=lambda i: (-scores[i], i))
        return [replace(head[i], rerank_score=scores[i]) for i in order] + ranked[n:]

    def _merge(
        self,
        dense_hits: Sequence[Hit],
        keyword_hits: Sequence[Hit],
        wanted: set[str],
        notes_loaded: bool,
    ) -> list[Passage]:
        """The two rankings merged by rank, best first."""
        s = self.settings
        merged: dict[str, dict] = {}
        for rank, hit in enumerate(dense_hits, start=1):
            merged.setdefault(hit.id, {"hit": hit})["dense"] = rank
        for rank, hit in enumerate(keyword_hits, start=1):
            merged.setdefault(hit.id, {"hit": hit})["keyword"] = rank

        passages = []
        for found in merged.values():
            hit = found["hit"]
            if (
                notes_loaded
                and hit.payload.get("kind") in SKILL_KINDS
                and hit.payload.get("capability") in wanted
            ):
                continue
            score = 0.0
            if "dense" in found:
                score += 1 / (s.rrf_k + found["dense"])
            if "keyword" in found:
                score += 1 / (s.rrf_k + found["keyword"])
            if hit.payload.get("capability") in wanted:
                score *= s.capability_boost
            if hit.payload.get("kind") == "class":
                score *= s.stub_penalty
            passages.append(
                (
                    score,
                    hit.id,
                    _passage(
                        hit,
                        score=score,
                        dense_rank=found.get("dense"),
                        keyword_rank=found.get("keyword"),
                    ),
                )
            )
        passages.sort(key=lambda t: (-t[0], t[1]))
        return [p for _, _, p in passages]

    def _search_clauses(
        self, question: str, ranked: list[Passage], wanted: set[str], notes_loaded: bool
    ) -> dict[tuple[str, int, int], list[str]]:
        """Searches each clause of `question` on its own and adds the best
        `clause_candidates` of each to `ranked` (in place) for the reranker.
        Returns, for each passage a clause found, the clauses that found it.
        A clause that fails to embed is skipped: the whole question's search
        still stands."""
        s = self.settings
        found_by: dict[tuple[str, int, int], list[str]] = {}
        if not s.clause_search or s.clause_candidates <= 0:
            return found_by
        present = {_key(p) for p in ranked}
        for clause in clauses(question, s.max_clauses):
            try:
                vector = self.embedder.embed_query(clause)
            except EmbeddingError as e:
                logger.warning("clause search skipped for one clause: %s", e)
                continue
            dense_hits, keyword_hits = self.store.search(vector, query_vector(clause), s.pool)
            for p in self._merge(dense_hits, keyword_hits, wanted, notes_loaded)[
                : s.clause_candidates
            ]:
                found_by.setdefault(_key(p), []).append(clause)
                if _key(p) not in present:
                    ranked.append(p)
                    present.add(_key(p))
        return found_by

    def _llm_rerank(
        self, question: str, candidates: list[Passage]
    ) -> tuple[list[Passage], int] | None:
        """`candidates` with the ones the chat model calls useful first, in
        its order, the rest after them as they were, and how many it called
        useful; None when there is no model or it fails. The skill slots are
        already among the candidates, so the model's order replaces them."""
        if self.llm_reranker is None or not candidates:
            return None
        try:
            useful = self.llm_reranker.useful(question, candidates)
        except LLMRerankError as e:
            logger.warning("llm rerank skipped, keeping the reranker's order: %s", e)
            return None
        rest = [i for i in range(len(candidates)) if i not in useful]
        return [candidates[i] for i in useful + rest], len(useful)

    def _rerank(
        self,
        question: str,
        ranked: list[Passage],
        found_by: dict[tuple[str, int, int], list[str]] | None = None,
    ) -> tuple[list[Passage], bool, set[tuple[str, int, int]]]:
        """The top of `ranked` reordered by the reranker, the rest after it
        unchanged; or `ranked` as it was when there is no reranker or it
        fails. A passage a clause found is also scored against that clause
        and keeps the better score: the passage for one clause of a long
        request reads as unrelated to the request as a whole. Also returns
        the best `clause_slots` passages of each clause by its own score,
        those scoring at least rerank_min_score."""
        s = self.settings
        n = s.rerank_candidates
        found_by = found_by or {}
        best_of_clause: set[tuple[str, int, int]] = set()
        if self.reranker is None or n <= 0 or not ranked:
            return ranked, False, best_of_clause
        # The top of the merged list, plus the top of each ranking on its
        # own. A chunk only one ranking finds scores half of one both find,
        # so the merge can sink a close meaning match -- a question worded
        # unlike the code it asks about -- below the reranker's cut.
        per_list = self.settings.rerank_per_ranking
        picked = [
            i
            for i, p in enumerate(ranked)
            if i < n
            or (p.dense_rank is not None and p.dense_rank <= per_list)
            or (p.keyword_rank is not None and p.keyword_rank <= per_list)
            or _key(p) in found_by
        ]
        head = [ranked[i] for i in picked]
        documents = [f"{p.context}\n\n{p.text}" for p in head]
        try:
            scores = self.reranker.rerank(question, documents)
        except RerankError as e:
            logger.warning("rerank skipped, keeping the hybrid order: %s", e)
            return ranked, False, best_of_clause
        for clause in dict.fromkeys(c for found in found_by.values() for c in found):
            mine = [j for j, p in enumerate(head) if clause in found_by.get(_key(p), ())]
            try:
                clause_scores = self.reranker.rerank(clause, [documents[j] for j in mine])
            except RerankError as e:
                logger.warning("rerank skipped for one clause: %s", e)
                continue
            for j, score in zip(mine, clause_scores, strict=True):
                scores[j] = max(scores[j], score)
            best = sorted(range(len(mine)), key=lambda k: -clause_scores[k])[: s.clause_slots]
            best_of_clause.update(
                _key(head[mine[k]]) for k in best if clause_scores[k] >= s.rerank_min_score
            )
        order = sorted(range(len(head)), key=lambda i: (-scores[i], i))
        rest = [p for i, p in enumerate(ranked) if i not in set(picked)]
        return (
            [replace(head[i], rerank_score=scores[i]) for i in order] + rest,
            True,
            best_of_clause,
        )

    def _drop_irrelevant(self, ranked: list[Passage], wanted: set[str]) -> list[Passage]:
        """`ranked` without what the reranker judged irrelevant or never
        read, keeping its best few whatever they scored and the named
        capabilities' skill notes. A passage that doesn't bear on the
        question only gives the model something to answer beside it."""
        s = self.settings
        return [
            p
            for i, p in enumerate(ranked)
            if i < s.rerank_keep
            or (p.rerank_score is not None and p.rerank_score >= s.rerank_min_score)
            or (p.kind in SKILL_KINDS and p.capability in wanted)
        ]

    @staticmethod
    def _pack(
        candidates: list[Passage], top_k: int, budget: int, held: Sequence[int] = ()
    ) -> list[Passage]:
        """The passages to return, best first. `held` are candidate
        positions taken before the rest; the result keeps rank order."""
        chosen: list[int] = []
        seen_text: set[str] = set()
        spent = 0

        def take(i: int) -> None:
            nonlocal spent
            passage = candidates[i]
            # The two rate limiters, for one, ship identical files.
            if passage.text in seen_text:
                return
            cost = prompt.passage_tokens(passage)
            if spent + cost > budget:
                return
            chosen.append(i)
            seen_text.add(passage.text)
            spent += cost

        for i in held:
            if len(chosen) >= top_k:
                break
            take(i)
        for i in range(len(candidates)):
            if len(chosen) >= top_k:
                break
            if i not in chosen:
                take(i)
        return [candidates[i] for i in sorted(chosen)]
