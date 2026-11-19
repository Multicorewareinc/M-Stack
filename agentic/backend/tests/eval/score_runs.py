"""One verdict per case from run_scripts.py output. No model and no RAGAS:
every comparison is exact.

    PYTHONPATH=src python tests/eval/score_runs.py rag_off.jsonl [rag_on.jsonl ...]

A case is right only if every recorded run of it is. A run is right when:

  build   the plan the agent ended on is the reference call, field for
          field (ragas_script_prepare.py's comparison), and the turn never
          paused
  ask     nothing was built, the reply asks for the missing value, and
          the turn never paused
  pause   the turn paused where it should, offering the case's options in
          order, and the conversation ended in the reference call

Several files side by side: the first is the baseline, and each later one
lists the cases it fixed and broke against it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from case_sets import ALL_CASES
from ragas_script_prepare import sample
from script_report import fixed_and_broken, verdicts

CASES = {c.id: c for c in ALL_CASES}
KINDS = ("build", "ask", "pause")


def kind(case) -> str:
    return "pause" if case.pauses else "ask" if case.asks else "build"


def result(case, record: dict) -> dict:
    """One recorded run of `case`, judged."""
    s = sample(case, record)
    failure = s["failure"]
    offered = [tuple(p.get("options") or ()) for p in record.get("pauses") or []]
    if offered != list(case.pauses):
        paused = f"paused with {offered}" if offered else "did not pause"
        expected = f"expected {list(case.pauses)}" if case.pauses else "expected no pause"
        failure = " | ".join(filter(None, [failure, f"{paused} ({expected})"]))
    wanted = s["reference_context_ids"]
    recall = len(set(wanted) & set(s["retrieved_context_ids"])) / len(wanted) if wanted else None
    return {
        "id": case.id,
        "run": record.get("run", 1),
        "right": failure is None,
        "recall": recall,
        "failure": failure,
        "seconds": record.get("seconds"),
    }


def load(path: Path) -> list[dict]:
    """Every run in `path` that reached the agent, judged; the last record
    of a (case, run) wins, as it does for the runner."""
    records: dict[tuple[str, int], dict] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if "error" not in record and record.get("id") in CASES:
            records[(record["id"], record.get("run", 1))] = record
    order = {c.id: n for n, c in enumerate(ALL_CASES)}
    keys = sorted(records, key=lambda k: (order[k[0]], k[1]))
    return [result(CASES[k[0]], records[k]) for k in keys]


def summary(results: list[dict]) -> dict:
    v = verdicts(results)
    counts = {"cases": (sum(1 for c in v.values() if c.right), len(v))}
    for name in KINDS:
        ids = [i for i in v if kind(CASES[i]) == name]
        counts[name] = (sum(1 for i in ids if v[i].right), len(ids))
    counts["runs"] = (sum(1 for r in results if r["right"]), len(results))
    with_passage = [c for c in v.values() if c.found is not None]
    counts["found"] = (sum(1 for c in with_passage if c.found), len(with_passage))
    seconds = [r["seconds"] for r in results if r.get("seconds") is not None]
    return {"counts": counts, "seconds": sum(seconds) / len(seconds) if seconds else None, "verdicts": v}


def main() -> None:
    paths = [Path(p) for p in sys.argv[1:]]
    if not paths:
        sys.exit(__doc__)
    runs = [(p.stem, summary(load(p))) for p in paths]

    rows = [
        ("cases right in every run", "cases"),
        ("  build", "build"),
        ("  ask", "ask"),
        ("  pause", "pause"),
        ("runs right", "runs"),
        ("found its reference passage", "found"),
    ]
    print(f"{'':30}" + "".join(f"{name:>16}" for name, _ in runs))
    for label, key in rows:
        print(f"{label:30}" + "".join(f"{'%d/%d' % s['counts'][key]:>16}" for _, s in runs))
    print(
        f"{'mean seconds per run':30}"
        + "".join(f"{(f'{s['seconds']:.1f}' if s['seconds'] is not None else '-'):>16}" for _, s in runs)
    )

    base_name, base = runs[0]
    for name, other in runs[1:]:
        fixed, broke = fixed_and_broken(base["verdicts"], other["verdicts"])
        print(f"\n{name} against {base_name}: fixed {fixed or 'none'}, broke {broke or 'none'}")

    for name, s in runs:
        wrong = {i: c for i, c in s["verdicts"].items() if not c.right}
        print(f"\n{name}: {len(wrong)} case(s) not right in every run")
        for case_id, c in wrong.items():
            print(f"  {case_id} ({c.right_runs}/{c.runs} runs right)")
            for failure in c.failures:
                print(f"      {failure[:300]}")


if __name__ == "__main__":
    main()
