"""The RAGAS reference answers stay in step with the grounding cases."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "eval"))

from cases import GROUNDING_CASES
from references import REFERENCES


def test_every_grounding_case_has_a_reference_and_no_reference_is_orphaned():
    assert {c.id for c in GROUNDING_CASES} == set(REFERENCES)


def test_each_reference_states_the_fact_its_case_checks_for():
    missing = [
        c.id
        for c in GROUNDING_CASES
        if c.must_contain not in REFERENCES[c.id]
        and c.must_contain.split("=")[0] not in REFERENCES[c.id]
    ]
    assert not missing
