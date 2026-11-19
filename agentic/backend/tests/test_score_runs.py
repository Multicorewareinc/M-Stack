"""tests/eval/score_runs.py: the exact, model-free verdict per case."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "eval"))

import score_runs
from script_cases import ALL_CASES

CASES = {c.id: c for c in ALL_CASES}


def _record(case_id: str, *, args=None, pauses=(), reply="Done.", run=1, valid=True) -> dict:
    case = CASES[case_id]
    attempts = [{"tool": case.tool, "args": args if args is not None else case.expected, "valid": valid}]
    return {
        "id": case_id, "run": run, "attempts": attempts, "reply": reply, "sources": [],
        "script": "x = 1" if valid else None, "seconds": 10.0,
        "pauses": [{"question": "?", "options": list(options)} for options in pauses],
    }


def test_the_reference_call_with_no_pause_is_right():
    assert score_runs.result(CASES["valkey-chart-version"], _record("valkey-chart-version"))["right"]


def test_a_wrong_value_is_named():
    args = {"valkey": {**CASES["valkey-chart-version"].expected["valkey"], "name": "other"}}
    result = score_runs.result(CASES["valkey-chart-version"], _record("valkey-chart-version", args=args))
    assert not result["right"] and "valkey.name" in result["failure"]


def test_a_pause_nobody_needed_makes_a_right_plan_wrong():
    record = _record("valkey-chart-version", pauses=[("Create a new cluster", "Use an existing cluster")])
    result = score_runs.result(CASES["valkey-chart-version"], record)
    assert not result["right"] and "expected no pause" in result["failure"]


def test_a_pause_case_needs_both_its_pause_and_its_plan():
    case = CASES["pause-existing-cluster"]
    assert score_runs.result(case, _record(case.id, pauses=case.pauses))["right"]
    missed = score_runs.result(case, _record(case.id))
    assert not missed["right"] and "did not pause" in missed["failure"]
    wrong_options = score_runs.result(case, _record(case.id, pauses=[("new", "existing")]))
    assert not wrong_options["right"]


def test_an_ask_case_is_right_when_nothing_was_built_and_the_value_was_asked_for():
    case = CASES["ask-db-password"]
    asked = _record(case.id, valid=False, reply="What password should the appdb database use?")
    assert score_runs.result(case, asked)["right"]
    built = _record(case.id, valid=True)
    assert "built instead of asking" in score_runs.result(case, built)["failure"]


def test_a_case_is_right_only_if_every_run_is(tmp_path):
    good = _record("valkey-chart-version", run=1)
    bad = _record("valkey-chart-version", run=2, pauses=[("a", "b")])
    path = tmp_path / "run.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in (good, bad, {"id": "valkey-chart-version", "run": 3, "error": "x"})))
    summary = score_runs.summary(score_runs.load(path))
    assert summary["counts"]["cases"] == (0, 1) and summary["counts"]["runs"] == (1, 2)
    assert summary["counts"]["build"] == (0, 1) and summary["seconds"] == 10.0
