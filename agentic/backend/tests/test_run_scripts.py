"""Which runs run_scripts.py still has to do: every case's runs, all cases
once before any twice, less those already recorded -- except a run that
never reached the agent, which is redone. And how it stops a run whose
agent isn't started the way --setup says."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "eval"))

import httpx
from router import _ndjson
from run_scripts import NO_PASSAGE_LIMIT, Recorder, pending, run_case, wrong_setup
from script_cases import ALL_CASES

IDS = [c.id for c in ALL_CASES]
CASES = {c.id: c for c in ALL_CASES}


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _write(path: Path, records: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def _ids(todo) -> list[tuple[int, str]]:
    return [(run, case.id) for run, case in todo]


def test_every_case_runs_once_before_any_runs_twice(tmp_path):
    todo = _ids(pending(tmp_path / "run.jsonl", repeat=2))
    assert todo == [(1, i) for i in IDS] + [(2, i) for i in IDS]


def test_recorded_runs_are_skipped_and_unreached_ones_redone(tmp_path):
    out = _write(
        tmp_path / "run.jsonl",
        [
            {"id": IDS[0], "run": 1, "attempts": []},
            {"id": IDS[1], "run": 1, "error": "connection refused"},
            # Recorded before runs were numbered: run 1.
            {"id": IDS[2], "attempts": []},
        ],
    )
    todo = _ids(pending(out, repeat=1))
    assert (1, IDS[0]) not in todo and (1, IDS[2]) not in todo
    assert (1, IDS[1]) in todo


def test_only_limits_the_cases(tmp_path):
    todo = _ids(pending(tmp_path / "run.jsonl", repeat=3, only=[IDS[0]]))
    assert todo == [(1, IDS[0]), (2, IDS[0]), (3, IDS[0])]


def test_a_run_records_what_the_agents_stream_carries():
    # Rendered by the agent's own router, so a renamed field shows here.
    events = [
        {"type": "sources", "sources": [{"path": "a.py", "start": 1, "end": 2}]},
        {"type": "tool_call", "tool": "build_full_stack_plan", "args": {"x": 1}},
        {"type": "tool_result", "valid": False, "error": "needs the database password"},
        {"type": "final", "reply": "What password should appdb use?", "script": None},
    ]

    async def stream():
        for event in events:
            yield event

    async def render():
        return "".join([chunk async for chunk in _ndjson("session", stream())])

    body = asyncio.run(render())
    client = httpx.Client(
        base_url="http://agent",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body)),
    )
    record = run_case(client, "Add a model gateway.")
    assert record["sources"] == ["a.py:1-2"]
    assert record["attempts"][0]["valid"] is False
    assert record["reply"] == "What password should appdb use?"


def _conversation(turns: dict[str, list[dict]]):
    """A client whose agent answers each message with the events scripted
    for it, and the (path, message) of every request it received."""
    seen: list[tuple[str, str]] = []

    async def _render(events: list[dict]) -> str:
        async def stream():
            for event in events:
                yield event

        return "".join([chunk async for chunk in _ndjson("sess-1", stream())])

    def handler(request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content)["message"]
        seen.append((request.url.path, message))
        return httpx.Response(200, text=asyncio.run(_render(turns[message])))

    return httpx.Client(base_url="http://agent", transport=httpx.MockTransport(handler)), seen


def test_a_pause_is_recorded_and_answered_on_the_same_session():
    client, seen = _conversation({
        "Add a cache.": [
            {"type": "tool_call", "tool": "build_valkey_plan", "args": {"valkey": {"name": "s"}}},
            {"type": "tool_result", "valid": False, "error": "asked", "needs": {"values": [], "choices": ["cluster"], "fixes": 0}},
            {"type": "ask_user", "question": "New or existing?", "options": ["Create a new cluster", "Use an existing cluster"]},
        ],
        "Use an existing cluster": [
            {"type": "final", "reply": "Where is its kubeconfig?", "script": None},
        ],
        "It is /k.yaml": [
            {"type": "tool_call", "tool": "build_valkey_plan", "args": {"valkey": {"name": "s", "kubeconfig_path": "/k.yaml"}}},
            {"type": "tool_result", "valid": True, "error": None},
            {"type": "final", "reply": "Here it is.", "script": "cache = Cache()"},
        ],
    })
    record = run_case(client, "Add a cache.", ("Use an existing cluster", "It is /k.yaml", "never said"))
    assert [path for path, _ in seen] == [
        "/v1/sessions/stream",
        "/v1/sessions/sess-1/messages/stream",
        "/v1/sessions/sess-1/messages/stream",
    ]
    assert [message for _, message in seen] == ["Add a cache.", "Use an existing cluster", "It is /k.yaml"]
    assert record["pauses"] == [
        {"question": "New or existing?", "options": ["Create a new cluster", "Use an existing cluster"]}
    ]
    assert record["replies"] == ["New or existing?", "Where is its kubeconfig?", "Here it is."]
    assert record["script"] == "cache = Cache()"
    assert record["attempts"][0]["needs"] == {"values": [], "choices": ["cluster"], "fixes": 0}


def test_answers_are_not_sent_once_a_script_exists():
    client, seen = _conversation({
        "Add a cache on /k.yaml.": [{"type": "final", "reply": "Done.", "script": "cache = Cache()"}],
    })
    record = run_case(client, "Add a cache on /k.yaml.", ("Use an existing cluster",))
    assert len(seen) == 1 and record["pauses"] == [] and record["script"]


def test_every_pause_case_is_run_with_its_answers(tmp_path):
    todo = pending(tmp_path / "run.jsonl", repeat=1, only=["pause-new-cluster"])
    assert [(run, case.id, len(case.answers)) for run, case in todo] == [(1, "pause-new-cluster", 2)]


def test_a_passage_in_an_off_run_is_the_wrong_setup():
    case = CASES["gateway-fail-open"]
    assert wrong_setup("off", case, []) is None
    assert wrong_setup("off", case, ["multistack/gateway/spec.py:134-136"])


def test_a_perfect_run_must_get_exactly_the_cases_own_passages():
    case = CASES["gateway-fail-open"]
    assert wrong_setup("perfect", case, list(case.reference_context_ids)) is None
    assert wrong_setup("perfect", case, [])
    assert wrong_setup("perfect", case, [*case.reference_context_ids, "a.py:1-2"])
    # A case with no reference passage gets none, as with RAG off.
    assert wrong_setup("perfect", CASES["chain-gateway"], []) is None


def _on_run(n: int, sources: list[str]) -> dict:
    return {"id": IDS[n], "run": 1, "sources": sources, "attempts": []}


def test_an_on_run_without_passages_waits_for_one_with_them(tmp_path):
    out = tmp_path / "run.jsonl"
    recorder = Recorder(out, "on")
    assert recorder.add(_on_run(0, [])) is None
    assert _lines(out) == []
    assert recorder.add(_on_run(1, ["a.py:1-2"])) is None
    assert [line["id"] for line in _lines(out)] == [IDS[0], IDS[1]]


def test_on_runs_in_a_row_without_passages_stop_the_run_unrecorded(tmp_path):
    out = tmp_path / "run.jsonl"
    recorder = Recorder(out, "on")
    problems = [recorder.add(_on_run(n, [])) for n in range(NO_PASSAGE_LIMIT)]
    assert problems[:-1] == [None] * (NO_PASSAGE_LIMIT - 1) and problems[-1]
    recorder.close()
    assert _lines(out) == []


def test_runs_held_at_the_end_are_recorded_when_the_run_finishes(tmp_path):
    out = tmp_path / "run.jsonl"
    recorder = Recorder(out, "on")
    recorder.add(_on_run(0, ["a.py:1-2"]))
    recorder.add(_on_run(1, []))
    recorder.close()
    assert [line["id"] for line in _lines(out)] == [IDS[0], IDS[1]]
