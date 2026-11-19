"""Scores cases.py against TODAY's agent -- no model call, no server
needed. This is the number every future change gets compared to.

    PYTHONPATH=src python tests/eval/score.py

Two things this does NOT tell you, on purpose: whether the model, given
the right notes, actually writes a correct answer (that needs a live
model call -- see run_live.py), and whether a generated script validates
(that's the tool layer's own job, covered by the main test suite, not
this one). This only asks the two free, instant, always-current
questions: did the right notes load, and is the fact present anywhere
in them.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases import ALL_ROUTING, GROUNDING_CASES, SCOPE_CASES  # noqa: E402  -- SCOPE_CASES re-exported for run_live.py

from orchestration.knowledge import OUT_OF_SCOPE_REPLY, load_skills  # noqa: E402
from orchestration.selection import select, select_skills  # noqa: E402

SKILL_TEXT = dict(load_skills())


def score_routing() -> tuple[int, int, list[str]]:
    """For each case, does selection load every skill the case needs?
    Extra skills loaded alongside are fine -- only a MISSING one fails,
    the same standard the agent itself has to meet."""
    passed = failures = 0
    lines = []
    for case in ALL_ROUTING:
        skills, _tools = select(case.text, [], budget=100_000)
        missing = [s for s in case.needs if s not in skills]
        ok = not missing
        passed += ok
        failures += not ok
        lines.append(f"{'ok  ' if ok else 'FAIL'} {case.id:28} missing={missing or '-'}")
    return passed, passed + failures, lines


def score_grounding() -> tuple[int, int, list[str]]:
    """Is `must_contain` present in the text of every skill the case
    names? A case with several skills passes if the fact is in ANY of
    them -- the model only needs to find it once."""
    passed = failures = 0
    lines = []
    for case in GROUNDING_CASES:
        found = any(case.must_contain in SKILL_TEXT.get(s, "") for s in case.skills)
        ok = found
        passed += ok
        failures += not ok
        flag = " (known gap)" if case.known_gap else ""
        lines.append(f"{'ok  ' if ok else 'FAIL'} {case.id:28} contains {case.must_contain!r}{flag}")
    return passed, passed + failures, lines


def score_scope() -> None:
    """Deliberately NOT scored here. Whether a request gets refused is a
    decision the MODEL makes from the system prompt (see
    orchestration/knowledge.py's OUT_OF_SCOPE_REPLY) -- nothing in
    selection.py decides it, so there is no free, code-only signal to
    check it against. An earlier version of this function used "does the
    request name a component" as a stand-in and it was wrong on its own
    first case ("hi" is in scope and names no component) -- a bad proxy
    that fails is worse than no check, because it looks like a real
    result. Score SCOPE_CASES for real in run_live.py, against a live
    model call; see tests/test_scope.py for the code-level guarantees
    that don't need a live call (that the scope rule is present in every
    prompt, worded first, etc.)."""


def main() -> None:
    print("=" * 70)
    print("ROUTING -- did the right notes load")
    print("=" * 70)
    p, t, lines = score_routing()
    print("\n".join(lines))
    print(f"\nROUTING: {p}/{t}\n")

    print("=" * 70)
    print("GROUNDING -- is the fact present in what we hand the model today")
    print("=" * 70)
    p2, t2, lines = score_grounding()
    print("\n".join(lines))
    known_gaps = sum(1 for c in GROUNDING_CASES if c.known_gap)
    print(f"\nGROUNDING: {p2}/{t2}  ({known_gaps} are known gaps, expected to fail until RAG-over-source exists)\n")

    print("=" * 70)
    print(f"SCOPE: {len(SCOPE_CASES)} cases written, not scorable without a live model call -- see run_live.py")
    print("=" * 70)

    print("=" * 70)
    print(f"BASELINE (code-only)  routing={p}/{t}  grounding={p2}/{t2}")
    print("=" * 70)


if __name__ == "__main__":
    main()
