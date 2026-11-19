"""Per-case verdicts from per-run scores, for ragas_script_score.py: a case
is right only if every run of it is, and RAG found its passage only if it
did in every run. Kept apart from the scorer so it is tested without RAGAS.

Each result is one scored run: {"id", "run", "right", "recall", "failure"},
`recall` None for a case with no reference passage.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True)
class Verdict:
    runs: int
    right_runs: int
    # None when the case has no reference passage to find.
    found_runs: int | None
    # One per wrong run: "run N: why".
    failures: tuple[str, ...]

    @property
    def right(self) -> bool:
        return self.right_runs == self.runs

    @property
    def found(self) -> bool | None:
        return None if self.found_runs is None else self.found_runs == self.runs


def verdicts(results: list[dict]) -> dict[str, Verdict]:
    by_case: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_case[r["id"]].append(r)
    out = {}
    for case_id, runs in by_case.items():
        recalls = [r["recall"] for r in runs if r["recall"] is not None]
        out[case_id] = Verdict(
            runs=len(runs),
            right_runs=sum(1 for r in runs if r["right"]),
            found_runs=sum(1 for x in recalls if x == 1.0) if recalls else None,
            failures=tuple(
                f"run {r['run']}: {r.get('failure') or 'differs'}" for r in runs if not r["right"]
            ),
        )
    return out


def fixed_and_broken(
    base: dict[str, Verdict], other: dict[str, Verdict]
) -> tuple[list[str], list[str]]:
    """Cases `other` gets right that `base` doesn't, and the reverse, over
    the cases both ran."""
    shared = [c for c in base if c in other]
    return (
        [c for c in shared if other[c].right and not base[c].right],
        [c for c in shared if base[c].right and not other[c].right],
    )
