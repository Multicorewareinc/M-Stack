"""How script_report.py turns scored runs into per-case verdicts: right
only if every run is, found only if every run found the passage, and the
cases one setup fixed and broke against another."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "eval"))

from script_report import fixed_and_broken, verdicts


def _run(case_id: str, run: int, right: bool, recall: float | None = 1.0, failure=None) -> dict:
    return {"id": case_id, "run": run, "right": right, "recall": recall, "failure": failure}


def test_a_case_is_right_only_if_every_run_is():
    v = verdicts(
        [_run("a", 1, True), _run("a", 2, False, failure="no valid plan"), _run("a", 3, True)]
    )["a"]
    assert (v.right, v.right_runs, v.runs) == (False, 2, 3)
    assert v.failures == ("run 2: no valid plan",)


def test_rag_found_the_passage_only_if_it_did_in_every_run():
    v = verdicts([_run("a", 1, True, 1.0), _run("a", 2, True, 0.0)])["a"]
    assert (v.found, v.found_runs) == (False, 1)


def test_a_case_without_a_reference_passage_is_not_scored_for_finding_it():
    v = verdicts([_run("chain", 1, True, None), _run("chain", 2, True, None)])["chain"]
    assert v.found is None and v.right


def test_fixed_and_broken_compare_whole_cases_both_setups_ran():
    off = verdicts([_run("a", 1, False), _run("b", 1, True), _run("c", 1, True)])
    on = verdicts([_run("a", 1, True), _run("b", 1, False)])
    assert fixed_and_broken(off, on) == (["a"], ["b"])
