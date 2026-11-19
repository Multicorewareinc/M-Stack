"""Which evaluation set the runner and the scorers use.

    EVAL_CASES unset     the set the agent was developed against (script_cases.py)
    EVAL_CASES=fresh     a generated set, from fresh_cases.py's own seed
    EVAL_CASES=fresh:7   the generated set for seed 7

A run and its scoring have to name the same set: run_all.sh passes its CASES
setting to both.
"""

from __future__ import annotations

import os

from script_cases import PAUSE_CASES as _PAUSE_CASES
from script_cases import SCRIPT_CASES as _SCRIPT_CASES
from script_cases import ScriptCase


def load(name: str | None = None) -> tuple[list[ScriptCase], list[ScriptCase]]:
    """(single-request cases, pause cases) of the set `name` selects;
    EVAL_CASES when `name` is not given."""
    name = (os.environ.get("EVAL_CASES", "") if name is None else name).strip()
    if not name:
        return list(_SCRIPT_CASES), list(_PAUSE_CASES)
    kind, _, seed = name.partition(":")
    if kind != "fresh" or (seed and not seed.isdigit()):
        raise SystemExit(f"EVAL_CASES={name!r}: expected `fresh` or `fresh:<seed>`")
    from fresh_cases import SEED, generate

    cases = generate(int(seed) if seed else SEED)
    return [c for c in cases if not c.pauses], [c for c in cases if c.pauses]


SCRIPT_CASES, PAUSE_CASES = load()
# What the runner runs and the scorers score.
ALL_CASES: list[ScriptCase] = [*SCRIPT_CASES, *PAUSE_CASES]
