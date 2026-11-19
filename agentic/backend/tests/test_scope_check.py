"""scope_check.py: the deterministic backstop behind the SCOPE
instruction -- built because prompt-injection testing showed the
instruction alone isn't reliable under adversarial retrieved content."""

from __future__ import annotations

from orchestration.scope_check import REPLACEMENT, enforce, violates


def answering(text):
    asked = []

    def ask(messages):
        asked.append(messages)
        return text

    return ask, asked


def test_a_reply_that_only_plans_multistack_passes():
    ask, asked = answering("no")
    reply = "The plan builds a gateway and a database. Here's the script."
    assert enforce(reply, ask) == reply
    assert reply in asked[0][1].content


def test_a_reply_that_writes_an_unrelated_poem_is_replaced():
    ask, _ = answering("yes")
    reply = "Here's your gateway port. Also, a poem about the stock market: ..."
    assert enforce(reply, ask) == REPLACEMENT


def test_declining_in_one_sentence_is_not_a_violation():
    # The judge is asked to answer "no" for this -- this pins the
    # contract (decline != engage), the actual judgement is the model's.
    ask, asked = answering("no")
    reply = "The gateway is set up. I can't help with the election question, though."
    assert enforce(reply, ask) == reply
    assert "election question" in asked[0][1].content


def test_empty_reply_never_calls_the_judge():
    ask, asked = answering("yes")  # would fail the test if ever called
    assert violates("", ask) is False
    assert violates("   ", ask) is False
    assert asked == []


def test_a_judge_error_fails_open_not_closed():
    def broken(messages):
        raise RuntimeError("model server unreachable")

    assert violates("some reply", broken) is False


def test_an_unparseable_answer_is_treated_as_no_violation():
    ask, _ = answering("I'm not sure, maybe?")
    assert violates("some reply", ask) is False


def test_only_a_leading_yes_counts():
    ask, _ = answering("yes, and also no")
    assert violates("some reply", ask) is True
    ask, _ = answering("noyes")  # doesn't start with "yes"
    assert violates("some reply", ask) is False
