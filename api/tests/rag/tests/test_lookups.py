"""Exact lookups: a component's SDK fields read by label, and the service
code that works with one field's value."""

from __future__ import annotations

import math

import pytest
from chunking import chunk_python
from conftest import AUTH, KEY, MemoryStore
from corpus import Chunk, origin
from fastapi.testclient import TestClient
from indexer import build_index
from main import create_app
from chunking import estimate_tokens
from prompt import render_fields
from records import Record
from service import NotIndexed, Searcher

from settings import Settings

DIM = 4


def axis(i: int) -> list[float]:
    return [1.0 if j == i else 0.0 for j in range(DIM)]


def field(path: str, line: int, symbol: str, text: str, capability: str) -> Chunk:
    name = symbol.rpartition(".")[2]
    return Chunk(
        id=f"{path}:{line}:field",
        text=text,
        context=f"field '{name}' of class {symbol.rpartition('.')[0]}, in {path}",
        capability=capability,
        source_path=path,
        start_line=line,
        end_line=line,
        kind="field",
        symbol=symbol,
    )


def code(path: str, line: int, text: str, capability: str, kind: str = "function") -> Chunk:
    return Chunk(f"{path}:{line}:{kind}", text, f"{kind} in {path}", capability, path, line, line, kind)


class Embedder:
    """Chunks get the vector chosen for their text; every query points along
    axis 0. Records the queries, and how many calls embedded them."""

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self.vectors = vectors
        self.queries: list[str] = []
        self.calls = 0

    def embed_documents(self, chunks, *, on_batch=None):
        return [self.vectors.get(c.text, axis(3)) for c in chunks]

    def embed_query(self, question):
        self.calls += 1
        self.queries.append(question)
        return axis(0)

    def embed_queries(self, questions):
        self.calls += 1
        self.queries += list(questions)
        return [axis(0) for _ in questions]


def searcher(chunks, vectors=None, *, reranker=None, **settings) -> tuple[Searcher, Embedder]:
    store = MemoryStore()
    embedder = Embedder(vectors or {})
    build_index(chunks, store, embedder, model="m", dimension=DIM)
    embedder.calls, embedder.queries = 0, []
    config = {"min_score": 0.0, "min_query_terms": 0, **settings}
    return Searcher(store, embedder, Settings(**config), reranker), embedder


# ---- what is stored


def test_a_class_field_chunk_carries_its_class_and_field_as_a_symbol():
    source = (
        "class Outer:\n"
        "    # how many\n"
        "    replicas: int = 2\n"
        "    class Options:\n"
        "        chart: str = 'x'\n"
    )
    chunks = {c.context.split(",")[0]: c for c in chunk_python(source, "multistack/a/spec.py")}
    assert chunks["field 'replicas' of class Outer"].symbol == "Outer.replicas"
    assert chunks["field 'chart' of class Outer.Options"].symbol == "Outer.Options.chart"
    assert chunks["class Outer"].symbol is None


def test_origin_tells_the_sdk_from_the_services_and_the_skill_notes():
    assert origin("multistack/gateway/spec.py") == "sdk"
    assert origin("api/microservices/tokenizer/src/service.py") == "service"
    assert origin("agentic/backend/src/skills/gateway/SKILL.md") == "skill"
    assert origin("README.md") == "other"


def test_a_stored_field_is_findable_by_its_bare_name_and_origin():
    record = Record.from_chunk(
        field("multistack/gateway/spec.py", 9, "Gateway.policy_fail_mode", "x", "gateway"), "m"
    )
    payload = record.payload()
    assert (payload["symbol"], payload["field_name"], payload["origin"]) == (
        "Gateway.policy_fail_mode",
        "policy_fail_mode",
        "sdk",
    )
    unnamed = Record.from_chunk(code("api/microservices/x/main.py", 1, "y", "x"), "m").payload()
    assert (unnamed["symbol"], unnamed["field_name"], unnamed["origin"]) == ("", "", "service")


# ---- /v1/fields: a component's fields, by label

GATEWAY = "multistack/gateway/spec.py"
CACHE = "multistack/cache/spec.py"
FIELDS = [
    field(GATEWAY, 20, "Gateway.policy_fail_mode", 'policy_fail_mode: str = "closed"', "gateway"),
    field(GATEWAY, 10, "Gateway.replicas", "replicas: int = 2", "gateway"),
    field(GATEWAY, 5, "Gateway.CAPABILITY", 'CAPABILITY = "gateway"', "gateway"),
    code(GATEWAY, 30, "def validate(self): ...", "gateway"),
    field(CACHE, 4, "Cache.name", "name: str", "valkey"),
    field("multistack/tokenizer/spec.py", 4, "Tokenizer.replicas", "replicas: int = 1", "tokenizer"),
    field(
        "api/microservices/model-gateway/src/settings.py",
        42,
        "Settings.policy_fail_mode",
        'policy_fail_mode: str = "closed"',
        "gateway",
    ),
]


def test_fields_are_the_named_components_sdk_fields_the_first_named_first():
    s, embedder = searcher(FIELDS)
    result = s.fields(["valkey", "gateway"])
    assert [(p.path, p.start) for p in result.passages] == [
        (CACHE, 4),
        (GATEWAY, 10),
        (GATEWAY, 20),
    ]
    assert embedder.calls == 0
    assert result.render().startswith("Reference material")


def test_names_narrow_fields_to_those_fields():
    s, _ = searcher(FIELDS)
    result = s.fields(["gateway", "tokenizer"], names=["replicas"])
    assert [(p.path, p.start) for p in result.passages] == [
        (GATEWAY, 10),
        ("multistack/tokenizer/spec.py", 4),
    ]


def test_a_small_budget_keeps_the_first_component_whole_before_the_next():
    s, _ = searcher(FIELDS)
    first = s.fields(["valkey"]).passages
    budget = estimate_tokens(render_fields(first))
    result = s.fields(["valkey", "gateway"], budget_tokens=budget)
    assert [p.path for p in result.passages] == [CACHE]


def test_fields_render_one_heading_per_class_with_its_fields_as_in_the_source():
    s, _ = searcher(FIELDS)
    text = render_fields(s.fields(["gateway"]).passages)
    assert text.count("### ") == 1
    assert f"### {GATEWAY}:10-20 -- class Gateway\nreplicas: int = 2\npolicy_fail_mode" in text


def test_fields_on_an_empty_index_say_so():
    with pytest.raises(NotIndexed):
        Searcher(MemoryStore(), Embedder({}), Settings()).fields(["gateway"])


# ---- /v1/dependencies and /v1/tools: synthetic chunks, read the same way
# as fields -- by exact tag, no embedding call. See corpus.py's
# dependency_chunks()/tool_mapping_chunks(), which build these from
# mcp_tools/architecture.py's DEPENDS_ON/REASON and
# orchestration/selection.py's TOOL_FOR_SKILL.

ARCH = "agentic/backend/src/mcp_tools/architecture.py"
SEL = "agentic/backend/src/orchestration/selection.py"


def dependency(component: str, dep: str, text: str) -> Chunk:
    return Chunk(
        id=f"{ARCH}:dependency:{component}:{dep}",
        text=text,
        context=f"why the {component} capability depends on {dep}",
        capability=component,
        source_path=ARCH,
        start_line=1,
        end_line=1,
        kind="dependency",
        symbol=f"{component}->{dep}",
    )


def tool_mapping(skill: str, tool_name: str) -> Chunk:
    return Chunk(
        id=f"{SEL}:tool_mapping:{skill}",
        text=f"{skill} is built by the {tool_name} tool.",
        context=f"which MCP tool builds the {skill} capability on its own",
        capability=skill,
        source_path=SEL,
        start_line=1,
        end_line=1,
        kind="tool_mapping",
        symbol=tool_name,
    )


DEPENDENCIES = [
    dependency("policy", "gateway", "policy needs gateway: called on every request"),
    dependency("policy", "valkey", "policy needs valkey: keeps counters in Redis"),
    dependency("policy", "queue", "policy needs queue: counters advance from events"),
    dependency("gateway", "controlplane", "gateway needs controlplane: verifies API keys"),
]

TOOLS = [
    tool_mapping("gateway", "build_gateway_plan"),
    tool_mapping("policy", "build_policy_plan"),
]


def test_dependencies_are_the_named_components_direct_edges_only():
    s, embedder = searcher(DEPENDENCIES)
    result = s.dependencies(["policy"])
    assert {p.symbol for p in result.passages} == {
        "policy->gateway", "policy->valkey", "policy->queue",
    }
    # One hop only -- gateway's own dependency on controlplane is not
    # pulled in just because policy depends on gateway. The caller walks
    # multiple hops itself (selection.py's chain()).
    assert "gateway->controlplane" not in {p.symbol for p in result.passages}
    assert embedder.calls == 0


def test_dependencies_for_a_component_with_none_is_empty_not_missing():
    s, _ = searcher(DEPENDENCIES)
    result = s.dependencies(["valkey"])
    assert result.passages == []


def test_dependencies_on_an_empty_index_say_so():
    with pytest.raises(NotIndexed):
        Searcher(MemoryStore(), Embedder({}), Settings()).dependencies(["policy"])


def test_tools_for_returns_each_components_own_single_resource_tool():
    s, embedder = searcher(TOOLS)
    result = s.tools_for(["gateway", "policy"])
    assert {(p.capability, p.symbol) for p in result.passages} == {
        ("gateway", "build_gateway_plan"),
        ("policy", "build_policy_plan"),
    }
    assert embedder.calls == 0


def test_tools_on_an_empty_index_say_so():
    with pytest.raises(NotIndexed):
        Searcher(MemoryStore(), Embedder({}), Settings()).tools_for(["gateway"])


# ---- rank_skills(): a margin, not a raw score -- see corpus.py's
# skill_counterexample_chunks() and selection.py's FALSE_MATCHES.


def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


MIX01 = _normalize([1.0, 1.0, 0.0, 0.0])  # equally related to axis(0) and axis(1)


class QueryAwareEmbedder(Embedder):
    """Like Embedder, but embed_query returns a vector chosen per query
    text instead of always axis(0) -- needed to show two different
    requests scoring differently against the same counterexample."""

    def __init__(self, vectors, query_vectors):
        super().__init__(vectors)
        self.query_vectors = query_vectors

    def embed_query(self, question):
        self.calls += 1
        self.queries.append(question)
        return self.query_vectors[question]


def skill_intent(skill: str, text: str) -> Chunk:
    return Chunk(
        id=f"intent:{skill}", text=text, context=f"what the {skill} skill is for",
        capability=skill, source_path="x", start_line=1, end_line=1, kind="skill_intent",
    )


def counterexample(skill: str, text: str) -> Chunk:
    return Chunk(
        id=f"counter:{skill}", text=text, context="a sentence that looks like a match but isn't",
        capability=skill, source_path="x", start_line=1, end_line=1, kind="skill_counterexample",
    )


def test_rank_skills_subtracts_the_best_matching_counterexample():
    store = MemoryStore()
    vectors = {"policy trigger": axis(0), "fail open sentence": MIX01}
    query_vectors = {
        "worded like the known false match": MIX01,
        "a real rate-limiting request": axis(0),
    }
    embedder = QueryAwareEmbedder(vectors, query_vectors)
    build_index(
        [skill_intent("policy", "policy trigger"), counterexample("policy", "fail open sentence")],
        store, embedder, model="m", dimension=DIM,
    )
    s = Searcher(store, embedder, Settings(min_score=0.0, min_query_terms=0))

    [(_, false_margin)] = s.rank_skills("worded like the known false match")
    [(_, true_margin)] = s.rank_skills("a real rate-limiting request")

    # Pulled toward the counterexample (same direction as it) -- rejected.
    assert false_margin < 0
    # Not close to the counterexample, close to the real trigger -- kept.
    assert true_margin > 0
    assert true_margin > false_margin


def test_rank_skills_with_no_counterexamples_is_unchanged_a_raw_score():
    store = MemoryStore()
    vectors = {"storage trigger": axis(0)}
    query_vectors = {"a storage request": axis(0)}
    embedder = QueryAwareEmbedder(vectors, query_vectors)
    build_index([skill_intent("storage", "storage trigger")], store, embedder, model="m", dimension=DIM)
    s = Searcher(store, embedder, Settings(min_score=0.0, min_query_terms=0))

    [(capability, margin)] = s.rank_skills("a storage request")
    assert capability == "storage"
    assert margin == pytest.approx(1.0)  # no counterexample to subtract


def test_rank_skills_reorders_the_shortlist_by_a_real_reranker():
    # Two skills score identically on raw embedding margin (both axis(0)),
    # so a dense-only ranking can't tell them apart -- but the reranker
    # reads each skill's own text, and only one actually contains what
    # the user asked about.
    store = MemoryStore()
    vectors = {"gateway trigger mentions routing": axis(0), "policy trigger mentions limits": axis(0)}
    query_vectors = {"a request about limits": axis(0)}
    embedder = QueryAwareEmbedder(vectors, query_vectors)
    build_index(
        [
            skill_intent("gateway", "gateway trigger mentions routing"),
            skill_intent("policy", "policy trigger mentions limits"),
        ],
        store, embedder, model="m", dimension=DIM,
    )
    reranker = ByText("limits")
    s = Searcher(store, embedder, Settings(min_score=0.0, min_query_terms=0, skill_rerank_candidates=6), reranker)

    ranked = s.rank_skills("a request about limits")
    assert [capability for capability, _ in ranked] == ["policy", "gateway"]
    assert reranker.queries == ["a request about limits"]


def test_rank_skills_without_a_reranker_keeps_the_margin_order():
    store = MemoryStore()
    vectors = {"gateway trigger": axis(0), "policy trigger": axis(1)}
    query_vectors = {"mostly about gateway": _normalize([3.0, 1.0, 0.0, 0.0])}
    embedder = QueryAwareEmbedder(vectors, query_vectors)
    build_index(
        [skill_intent("gateway", "gateway trigger"), skill_intent("policy", "policy trigger")],
        store, embedder, model="m", dimension=DIM,
    )
    s = Searcher(store, embedder, Settings(min_score=0.0, min_query_terms=0))  # no reranker

    ranked = s.rank_skills("mostly about gateway")
    assert [capability for capability, _ in ranked] == ["gateway", "policy"]


# ---- /v1/uses: the service code that works with a value

TOKENIZER_SDK = field(
    "multistack/tokenizer/spec.py",
    47,
    "TiktokenOptions.default_encoding",
    'default_encoding: str = "cl100k_base"',
    "tokenizer",
)
REGISTRY = code(
    "api/microservices/tokenizer/src/service.py",
    23,
    'TOKENIZERS = {"cl100k_base": _encode_cl100k_base}',
    "tokenizer",
    kind="field",
)
SERVE = code("api/microservices/tokenizer/src/service.py", 33, "def count(self, text): ...", "tokenizer")
OTHER_SERVICE = code(
    "api/microservices/model-gateway/src/main.py", 3, 'TOKENIZERS = {"cl100k_base": 1}', "gateway"
)
SDK_ELSEWHERE = code("multistack/tokenizer/drivers/tiktoken.py", 9, "TOKENIZERS cl100k_base", "tokenizer")
USES = [TOKENIZER_SDK, REGISTRY, SERVE, OTHER_SERVICE, SDK_ELSEWHERE]


class ByText:
    """Scores a document by whether it holds `needle`."""

    def __init__(self, needle: str) -> None:
        self.needle = needle
        self.queries: list[str] = []

    def rerank(self, query, documents):
        self.queries.append(query)
        return [1.0 if self.needle in d else 0.0 for d in documents]


def test_uses_searches_only_that_capabilitys_service_code():
    s, _ = searcher(USES, {REGISTRY.text: axis(0), SERVE.text: axis(1)})
    [use] = s.uses([("tokenizer", "default_encoding", "o200k_base")])
    paths = {p.path for p in use.passages}
    assert paths == {"api/microservices/tokenizer/src/service.py"}
    assert (use.capability, use.name, use.value) == ("tokenizer", "default_encoding", "o200k_base")


def test_the_query_is_the_fields_own_sdk_chunk_with_the_value():
    s, embedder = searcher(USES)
    s.uses([("tokenizer", "default_encoding", "o200k_base")])
    [query] = embedder.queries
    assert TOKENIZER_SDK.text in query and query.endswith("value: o200k_base")


def test_several_values_are_embedded_in_one_call():
    s, embedder = searcher(USES)
    found = s.uses([("tokenizer", "default_encoding", "o200k_base"), ("gateway", "x", "1")])
    assert embedder.calls == 1 and len(found) == 2


def test_the_reranker_orders_what_uses_returns_and_drops_what_it_scores_low():
    reranker = ByText("def count")
    s, _ = searcher(USES, {REGISTRY.text: axis(0), SERVE.text: axis(1)}, reranker=reranker)
    [use] = s.uses([("tokenizer", "default_encoding", "o200k_base")])
    assert [p.start for p in use.passages] == [33]
    assert reranker.queries and TOKENIZER_SDK.text in reranker.queries[0]


def test_without_a_reranker_uses_keeps_the_first_few():
    s, _ = searcher(USES, {REGISTRY.text: axis(0), SERVE.text: axis(1)}, uses_top_k=1)
    [use] = s.uses([("tokenizer", "default_encoding", "o200k_base")])
    assert [p.start for p in use.passages] == [23]


# ---- the routes


def client(chunks, **settings) -> TestClient:
    store = MemoryStore()
    embedder = Embedder({})
    if chunks:
        build_index(chunks, store, embedder, model="m", dimension=DIM, commit="abc")
    config = Settings(service_api_key=KEY, **{"min_score": 0.0, "min_query_terms": 0, **settings})
    return TestClient(
        create_app(config, searcher=Searcher(store, embedder, config)),
        raise_server_exceptions=False,
    )


def test_fields_route_returns_passages_and_the_prompt_text():
    response = client(FIELDS).post("/v1/fields", json={"capabilities": ["valkey"]}, headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert [p["path"] for p in body["passages"]] == [CACHE]
    assert body["rendered"].startswith("Reference material")


def test_dependencies_route_returns_the_direct_edges():
    response = client(DEPENDENCIES).post(
        "/v1/dependencies", json={"capabilities": ["policy"]}, headers=AUTH
    )
    assert response.status_code == 200
    symbols = {p["symbol"] for p in response.json()["passages"]}
    assert symbols == {"policy->gateway", "policy->valkey", "policy->queue"}


def test_tools_route_returns_each_components_tool_name():
    response = client(TOOLS).post(
        "/v1/tools", json={"capabilities": ["gateway", "policy"]}, headers=AUTH
    )
    assert response.status_code == 200
    mapping = {p["capability"]: p["symbol"] for p in response.json()["passages"]}
    assert mapping == {"gateway": "build_gateway_plan", "policy": "build_policy_plan"}


def test_uses_route_returns_each_values_passages():
    response = client(USES).post(
        "/v1/uses",
        json={"items": [{"capability": "tokenizer", "name": "default_encoding", "value": "o200k_base"}]},
        headers=AUTH,
    )
    assert response.status_code == 200
    [use] = response.json()["uses"]
    assert use["name"] == "default_encoding" and use["passages"]
    assert all(p["path"].startswith("api/microservices/tokenizer/") for p in use["passages"])


def test_lookups_need_the_service_key():
    c = client(FIELDS)
    assert c.post("/v1/fields", json={"capabilities": ["valkey"]}).status_code == 401
    assert c.post("/v1/uses", json={"items": []}, headers=AUTH).status_code == 422
    assert c.post("/v1/dependencies", json={"capabilities": ["policy"]}).status_code == 401
    assert c.post("/v1/tools", json={"capabilities": ["gateway"]}).status_code == 401


def test_lookups_on_an_empty_index_are_503():
    c = client([])
    assert c.post("/v1/fields", json={"capabilities": ["x"]}, headers=AUTH).status_code == 503
    assert c.post("/v1/dependencies", json={"capabilities": ["x"]}, headers=AUTH).status_code == 503
    assert c.post("/v1/tools", json={"capabilities": ["x"]}, headers=AUTH).status_code == 503
