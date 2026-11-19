"""Shows where the selection pause would fire, so its threshold and band
can be set from scores instead of guessed.

    PYTHONPATH=src python tests/eval/selection_scores.py

For each request: the top two skill scores the RAG service returns, and
whether the top one sits within SELECTION_AMBIGUITY_BAND of
SKILL_MATCH_THRESHOLD (orchestration/selection.py), which is when the
agent stops to ask which component was meant.

Every evaluation request names what it wants, so none of them should
pause. The UNCLEAR requests below name nothing in particular and should.
The two settings are right when both hold; SKILL_MATCH_THRESHOLD and
SELECTION_AMBIGUITY_BAND in the environment try other values without a
code change.

Needs RAG_ENABLED and the RAG service. One embedding call and one rerank
call per request, to the shared model server, one at a time.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from script_cases import ALL_CASES

UNCLEAR = [
    "Set up the thing that sits in front of the models.",
    "We need something to keep usage under control.",
    "Add whatever is needed so people can log in and see their numbers.",
    "Make it store things.",
    "Can you add the monitoring or the billing piece, whichever tracks tokens?",
]


def row(label: str, text: str) -> tuple[str, bool] | None:
    from orchestration import grounding
    from orchestration.selection import SELECTION_AMBIGUITY_BAND, SKILL_MATCH_THRESHOLD

    ranked = sorted(grounding.relevant_skills(text), key=lambda pair: -pair[1])
    if not ranked:
        return None
    (top, score), rest = ranked[0], ranked[1:]
    pauses = abs(score - SKILL_MATCH_THRESHOLD) <= SELECTION_AMBIGUITY_BAND
    second = f"{rest[0][1]:.3f} {rest[0][0]}" if rest else "-"
    return f"{label:32} {score:.3f} {top:16} {second:24} {'PAUSES' if pauses else ''}", pauses


def main() -> None:
    from orchestration.selection import SELECTION_AMBIGUITY_BAND, SKILL_MATCH_THRESHOLD

    print(f"threshold {SKILL_MATCH_THRESHOLD}, band {SELECTION_AMBIGUITY_BAND}\n")
    print(f"{'request':32} {'top':22} {'next':24}")
    counts = {}
    for name, requests in (
        ("evaluation requests (should not pause)", [(c.id, c.request) for c in ALL_CASES]),
        ("unclear requests (should pause)", [(f"unclear-{n}", t) for n, t in enumerate(UNCLEAR, 1)]),
    ):
        paused = scored = 0
        for label, text in requests:
            result = row(label, text)
            if result is None:
                sys.exit("no scores came back: is RAG_ENABLED set and the RAG service running?")
            line, pauses = result
            print(line)
            paused += pauses
            scored += 1
        counts[name] = (paused, scored)
        print()
    for name, (paused, scored) in counts.items():
        print(f"{name}: {paused}/{scored} pause")


if __name__ == "__main__":
    main()
