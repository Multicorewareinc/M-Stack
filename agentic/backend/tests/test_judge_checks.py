"""tests/eval/judge_checks.py: the script that measures the judgement
calls. Offline, with stand-in judges, so what is pinned is the measuring
itself -- what counts as wrong, and that one wrong answer in several
repeats counts."""

from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "eval"))

import judge_checks
from orchestration import needs


def test_a_guard_that_gets_every_reply_right_reports_nothing():
    def judge(messages):
        return "yes" if messages[1].content in judge_checks.OFF_TOPIC else "no"

    assert judge_checks.check_scope(judge, judge_checks.ON_TOPIC, repeat=2) == ([], [])


def test_a_guard_that_flags_everything_is_reported_for_each_reply_it_replaced():
    replaced, let_through = judge_checks.check_scope(lambda m: "yes", judge_checks.ON_TOPIC, repeat=1)
    assert replaced == judge_checks.ON_TOPIC and let_through == []


def test_one_wrong_answer_in_several_repeats_counts():
    answers = itertools.cycle(["no", "no", "yes"])
    replaced, _ = judge_checks.check_scope(lambda m: next(answers), judge_checks.ON_TOPIC[:1], repeat=3)
    assert replaced == judge_checks.ON_TOPIC[:1]


def test_requests_that_state_the_type_in_the_skills_own_wording_are_never_judged():
    judged = []

    def judge(messages):
        judged.append(messages[1].content.split('"""')[1])  # the user's words alone
        return "[]"

    judge_checks.check_stated(judge, repeat=1)
    assert judged  # the other wordings do go to the judge
    assert not [text for text in judged if "requests per minute" in text or "tokens per minute" in text]


def test_a_stated_judge_that_answers_right_reports_nothing_and_a_wrong_one_is_listed():
    expected = dict((text, sorted(options)) for text, options in judge_checks.STATED)

    def right(messages):
        text = messages[1].content.split('"""')[1]
        return json.dumps(expected[text])

    assert judge_checks.check_stated(right, repeat=2) == []
    wrong = judge_checks.check_stated(lambda m: '["rpm"]', repeat=1)
    assert {text for text, _, _ in wrong} == {
        text for text, options in judge_checks.STATED
        if options != {"rpm"} and needs.stated_options(text, ("rpm", "tpm"), None, wordings={
            "rpm": ["requests per minute"], "tpm": ["tokens per minute"]}) is None
    }


def test_recorded_replies_come_from_every_turn_of_every_run(tmp_path):
    run = tmp_path / "run.jsonl"
    run.write_text(
        json.dumps({"id": "a", "reply": "old shape"}) + "\n"
        + json.dumps({"id": "b", "reply": "last", "replies": ["first", "last"]}) + "\n"
        + json.dumps({"id": "c", "error": "unreached"}) + "\n"
    )
    assert judge_checks.recorded_replies([run]) == ["old shape", "first", "last"]
