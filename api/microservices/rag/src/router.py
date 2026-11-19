"""HTTP routes.

- GET  /health      liveness (the process is up)
- GET  /ready       the vector database answers and something is indexed
- GET  /metrics     Prometheus
- POST /v1/search   the passages that bear on a question
- POST /v1/fields   the SDK fields of named components, read by label
- POST /v1/uses     the service code that works with given field values
- POST /v1/dependencies   the direct dependency edges of named components, read by label
- POST /v1/tools    which MCP tool builds each named component on its own, read by label
- POST /v1/relevant-skills   every skill, ranked by meaning against a request
- GET  /v1/status   what the index was built from
"""

from __future__ import annotations

import time

import prompt
from dependencies import require_service_key
from embeddings import EmbeddingError
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, Field
from service import NotIndexed
from store import StoreError

from errors import UnavailableError

router = APIRouter()

SEARCHES = Counter("rag_searches_total", "Searches by outcome", ["outcome"])
SEARCH_SECONDS = Histogram("rag_search_seconds", "Search latency")
RERANKS = Counter("rag_rerank_total", "Searches by how their results were ordered", ["outcome"])
LLM_RERANKS = Counter(
    "rag_llm_rerank_total", "Searches by whether the chat model chose the order", ["outcome"]
)
LOOKUPS = Counter(
    "rag_lookups_total", "Exact and scoped lookups by kind and outcome", ["kind", "outcome"]
)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    capabilities: list[str] = Field(default_factory=list, max_length=50)
    top_k: int | None = Field(default=None, ge=1, le=30)
    budget_tokens: int | None = Field(default=None, ge=100, le=50000)
    # The caller already has the named capabilities' skill notes in its
    # prompt: leave them out and spend the passages on source instead.
    notes_loaded: bool = False


class PassageOut(BaseModel):
    path: str
    start: int
    end: int
    context: str
    kind: str
    capability: str | None
    symbol: str | None = None
    score: float
    rerank_score: float | None = None
    text: str


class SearchResponse(BaseModel):
    passages: list[PassageOut]
    rendered: str
    best_similarity: float
    skipped: str | None = None
    reranked: bool = False
    llm_reranked: bool = False


class FieldsRequest(BaseModel):
    # Capability labels, the one the request named first.
    capabilities: list[str] = Field(min_length=1, max_length=50)
    names: list[str] = Field(default_factory=list, max_length=200)
    budget_tokens: int | None = Field(default=None, ge=100, le=50000)


class FieldsResponse(BaseModel):
    passages: list[PassageOut]
    rendered: str


class UseIn(BaseModel):
    capability: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    value: str = Field(max_length=500)


class UsesRequest(BaseModel):
    items: list[UseIn] = Field(min_length=1, max_length=30)
    top_k: int | None = Field(default=None, ge=1, le=10)


class UseOut(BaseModel):
    capability: str
    name: str
    value: str
    passages: list[PassageOut]


class UsesResponse(BaseModel):
    uses: list[UseOut]


class DependenciesRequest(BaseModel):
    # Capability labels to find the direct (one-hop) dependencies of.
    capabilities: list[str] = Field(min_length=1, max_length=50)


class DependenciesResponse(BaseModel):
    passages: list[PassageOut]


class ToolsRequest(BaseModel):
    capabilities: list[str] = Field(min_length=1, max_length=50)


class ToolsResponse(BaseModel):
    passages: list[PassageOut]


class RelevantSkillsRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class SkillScore(BaseModel):
    skill: str
    score: float


class RelevantSkillsResponse(BaseModel):
    skills: list[SkillScore]


@router.get("/health", tags=["ops"])
def health() -> dict:
    return {"status": "ok"}


@router.get("/ready", tags=["ops"])
def ready(request: Request) -> dict:
    searcher = request.app.state.searcher
    if not searcher.store.ready():
        raise UnavailableError("the vector database is not reachable")
    try:
        if searcher.meta() is None:
            raise UnavailableError("nothing has been indexed yet")
    except StoreError as e:
        raise UnavailableError(str(e)) from e
    return {"status": "ready"}


@router.get("/metrics", tags=["ops"])
def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.post(
    "/v1/search",
    response_model=SearchResponse,
    dependencies=[Depends(require_service_key)],
    tags=["search"],
)
def search(body: SearchRequest, request: Request) -> SearchResponse:
    """Plain `def`: the embedding and database calls block, and FastAPI runs
    a `def` route in its thread pool instead of on the event loop."""
    started = time.perf_counter()
    try:
        result = request.app.state.searcher.search(
            body.query,
            capabilities=body.capabilities,
            top_k=body.top_k,
            budget_tokens=body.budget_tokens,
            notes_loaded=body.notes_loaded,
        )
    except NotIndexed as e:
        SEARCHES.labels("error").inc()
        raise UnavailableError(str(e)) from e
    except (EmbeddingError, StoreError) as e:
        SEARCHES.labels("error").inc()
        raise UnavailableError(str(e)) from e
    SEARCH_SECONDS.observe(time.perf_counter() - started)
    SEARCHES.labels(
        "skipped" if result.skipped else "results" if result.passages else "empty"
    ).inc()
    if not result.skipped:
        searcher = request.app.state.searcher
        RERANKS.labels(
            "reranked" if result.reranked else "fallback" if searcher.reranker else "off"
        ).inc()
        LLM_RERANKS.labels(
            "reranked" if result.llm_reranked else "fallback" if searcher.llm_reranker else "off"
        ).inc()
    return SearchResponse(
        passages=[PassageOut(**p.source(), text=p.text) for p in result.passages],
        rendered=result.render(),
        best_similarity=round(result.best_similarity, 4),
        skipped=result.skipped,
        reranked=result.reranked,
        llm_reranked=result.llm_reranked,
    )


@router.post(
    "/v1/fields",
    response_model=FieldsResponse,
    dependencies=[Depends(require_service_key)],
    tags=["search"],
)
def fields(body: FieldsRequest, request: Request) -> FieldsResponse:
    try:
        result = request.app.state.searcher.fields(
            body.capabilities, names=body.names, budget_tokens=body.budget_tokens
        )
    except (NotIndexed, StoreError) as e:
        LOOKUPS.labels("fields", "error").inc()
        raise UnavailableError(str(e)) from e
    LOOKUPS.labels("fields", "results" if result.passages else "empty").inc()
    return FieldsResponse(
        passages=[PassageOut(**p.source(), text=p.text) for p in result.passages],
        rendered=prompt.render_fields(result.passages),
    )


@router.post(
    "/v1/uses",
    response_model=UsesResponse,
    dependencies=[Depends(require_service_key)],
    tags=["search"],
)
def uses(body: UsesRequest, request: Request) -> UsesResponse:
    """Plain `def` for the same reason as /v1/search: the calls block."""
    try:
        found = request.app.state.searcher.uses(
            [(i.capability, i.name, i.value) for i in body.items], top_k=body.top_k
        )
    except (NotIndexed, EmbeddingError, StoreError) as e:
        LOOKUPS.labels("uses", "error").inc()
        raise UnavailableError(str(e)) from e
    LOOKUPS.labels("uses", "results" if any(u.passages for u in found) else "empty").inc()
    return UsesResponse(
        uses=[
            UseOut(
                capability=u.capability,
                name=u.name,
                value=u.value,
                passages=[PassageOut(**p.source(), text=p.text) for p in u.passages],
            )
            for u in found
        ]
    )


@router.post(
    "/v1/dependencies",
    response_model=DependenciesResponse,
    dependencies=[Depends(require_service_key)],
    tags=["search"],
)
def dependencies(body: DependenciesRequest, request: Request) -> DependenciesResponse:
    """The direct (one-hop) dependency edges of `capabilities`, read by
    exact tag -- no embedding call. The caller walks these breadth-first
    for the full transitive closure (see orchestration/selection.py's chain())."""
    try:
        result = request.app.state.searcher.dependencies(body.capabilities)
    except (NotIndexed, StoreError) as e:
        LOOKUPS.labels("dependencies", "error").inc()
        raise UnavailableError(str(e)) from e
    LOOKUPS.labels("dependencies", "results" if result.passages else "empty").inc()
    return DependenciesResponse(
        passages=[PassageOut(**p.source(), text=p.text) for p in result.passages]
    )


@router.post(
    "/v1/tools",
    response_model=ToolsResponse,
    dependencies=[Depends(require_service_key)],
    tags=["search"],
)
def tools(body: ToolsRequest, request: Request) -> ToolsResponse:
    """Which MCP tool builds each of `capabilities` on its own, read by
    exact tag -- no embedding call."""
    try:
        result = request.app.state.searcher.tools_for(body.capabilities)
    except (NotIndexed, StoreError) as e:
        LOOKUPS.labels("tools", "error").inc()
        raise UnavailableError(str(e)) from e
    LOOKUPS.labels("tools", "results" if result.passages else "empty").inc()
    return ToolsResponse(passages=[PassageOut(**p.source(), text=p.text) for p in result.passages])


@router.post(
    "/v1/relevant-skills",
    response_model=RelevantSkillsResponse,
    dependencies=[Depends(require_service_key)],
    tags=["search"],
)
def relevant_skills(body: RelevantSkillsRequest, request: Request) -> RelevantSkillsResponse:
    """Plain `def`: the embedding call blocks, same reason as /v1/search."""
    try:
        ranked = request.app.state.searcher.rank_skills(body.text)
    except NotIndexed as e:
        LOOKUPS.labels("relevant_skills", "error").inc()
        raise UnavailableError(str(e)) from e
    except (EmbeddingError, StoreError) as e:
        LOOKUPS.labels("relevant_skills", "error").inc()
        raise UnavailableError(str(e)) from e
    LOOKUPS.labels("relevant_skills", "results" if ranked else "empty").inc()
    return RelevantSkillsResponse(
        skills=[SkillScore(skill=s, score=round(sc, 4)) for s, sc in ranked]
    )


@router.get("/v1/status", dependencies=[Depends(require_service_key)], tags=["search"])
def status(request: Request) -> dict:
    try:
        meta = request.app.state.searcher.meta()
    except StoreError as e:
        raise UnavailableError(str(e)) from e
    if meta is None:
        raise UnavailableError("nothing has been indexed yet")
    return {
        k: meta.get(k)
        for k in (
            "model",
            "dimension",
            "chunks",
            "commit",
            "commit_modified",
            "collection",
            "built_at",
        )
    }
