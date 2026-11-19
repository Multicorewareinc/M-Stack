"""Scores a run_scripts.py run against the reference scripts.

    PYTHONPATH=src python tests/eval/score_scripts.py run.jsonl [other_run.jsonl ...]

Per case, and as totals:
  script     a validated script was produced in the one turn
  chain      it builds exactly the components of the expected plan
  fields     it sets every line the request asked for
  match      share of the reference script's lines it contains, so a
             wrong value anywhere, defaults included, costs a line
  first      the first plan attempt was accepted, with no retry
  pause      a pause case stopped where it should and offered the right
             options, in order (blank for a case with no pause)
Several runs side by side compare configurations, e.g. retrieval off and on.

Build cases only, first run of each: ragas_script_score.py scores every
run and the ask cases too.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from case_sets import ALL_CASES
from script_cases import reference_script

_BUILT = re.compile(r"stack\.build\(\s*\n\s*(\w+),")


def built_components(script: str) -> set[str]:
    return set(_BUILT.findall(script))


def line_match(reference: str, script: str) -> float:
    wanted = Counter(line.strip() for line in reference.splitlines() if line.strip())
    have = Counter(line.strip() for line in script.splitlines() if line.strip())
    return sum((wanted & have).values()) / sum(wanted.values())


def score(
    record: dict, reference: str, must_set: tuple[str, ...], pauses: tuple[tuple[str, ...], ...] = ()
) -> dict:
    script = record.get("script") or ""
    attempts = record.get("attempts") or []
    plans = [a for a in attempts if a.get("tool", "").startswith("build_")]
    offered = [tuple(p.get("options") or ()) for p in record.get("pauses") or []]
    return {
        "script": bool(script),
        "chain": bool(script) and built_components(script) == built_components(reference),
        "fields": bool(script) and all(line in script for line in must_set),
        "match": line_match(reference, script) if script else 0.0,
        "first": bool(plans) and plans[0].get("valid") is True,
        # A case with no pause must not pause either.
        "pause": offered == list(pauses),
        "attempts": len(plans),
        "seconds": record.get("seconds"),
    }


def load(path: Path) -> dict[str, dict]:
    records = (json.loads(line) for line in path.read_text().splitlines() if line.strip())
    return {r["id"]: r for r in records if "error" not in r and r.get("run", 1) == 1}


def main() -> None:
    runs = [(Path(p).stem, load(Path(p))) for p in sys.argv[1:]]
    if not runs:
        sys.exit(__doc__)
    cases = [c for c in ALL_CASES if not c.asks]
    references = {c.id: reference_script(c) for c in cases}
    columns = ("script", "chain", "fields", "match", "first", "pause")

    totals = {name: Counter() for name, _ in runs}
    print(f"{'case':28}" + "".join(f"{name:>34}" for name, _ in runs))
    for case in cases:
        cells = []
        for name, records in runs:
            if case.id not in records:
                cells.append(f"{'(not run)':>34}")
                continue
            s = score(records[case.id], references[case.id], case.must_set, case.pauses)
            for key in columns:
                totals[name][key] += s[key]
            totals[name]["cases"] += 1
            totals[name]["attempts"] += s["attempts"]
            flags = "".join("Y" if s[k] else "." for k in ("script", "chain", "fields", "first", "pause"))
            cells.append(
                f"{flags} match={s['match']:.2f} tries={s['attempts']} {s['seconds']}s".rjust(34)
            )
        print(f"{case.id:28}" + "".join(cells))

    print("\nflags: script, chain, fields, first-try accepted, paused only where it should")
    for name, t in totals.items():
        n = t["cases"] or 1
        print(
            f"{name}: {t['cases']} cases  script {t['script']}/{n}  chain {t['chain']}/{n}  "
            f"fields {t['fields']}/{n}  first-try {t['first']}/{n}  pauses {t['pause']}/{n}  "
            f"mean match {t['match'] / n:.3f}  plan attempts {t['attempts']}"
        )


if __name__ == "__main__":
    main()
