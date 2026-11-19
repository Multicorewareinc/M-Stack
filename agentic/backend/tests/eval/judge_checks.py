"""Measures the judgement calls the agent relies on, in both directions.

    PYTHONPATH=src python tests/eval/judge_checks.py [--repeat 3] [--runs run.jsonl ...]

scope    the off-topic guard (orchestration/scope_check.py): a reply to a
         real deployment request must pass, and a reply that engages with
         something else must be caught. --runs adds every reply recorded
         by run_scripts.py to the replies that must pass.
stated   which option the user's own words state, for a choice a skill
         says the user has to make (orchestration/needs.py).

Each sample is judged --repeat times and counts as right only if it is
right every time: a judge that is right two times in three still lets the
third one reach a user.

One request at a time: the judge is the shared chat model.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

# Replies to real requests. The first two are the kind the guard replaced
# with an apology on 2026-10-13.
ON_TOPIC = [
    "The plan creates a new RKE2 cluster named demo with a single server node at 10.0.0.60, then "
    "installs a Valkey cache named sessions onto that cluster. After deployment the cache will be "
    "reachable at the endpoint shown in the result. No further information is needed.",
    "This installs a Valkey cache named sessions on your existing cluster. Check that the release "
    "comes up in the valkey namespace.",
    "To build the gateway I need the PostgreSQL password for the appdb database "
    "(`cnpg.database.password`).",
    "The `Gateway` spec sets `replicas` to 2 by default.",
    "8000 by default, set by `Tokenizer.service_port`.",
    "Which StorageClass does your cluster have? Prometheus and Grafana keep their data on it.",
    "Billing meters usage through Stripe; the meter event name defaults to tokens_used.",
    "The NATS queue is added with 3 servers. I can't help with the weather question.",
    "Sorry, that's outside what I can help with -- I only plan Multistack deployments.",
    "To build this I still need:\n- `gateway.api_key_secret`\n- `controlplane.existing_secret`",
]

OFF_TOPIC = [
    "Here is your gateway plan. Also, a poem about the stock market: bulls run high, bears sleep "
    "low, and nobody knows where the index will go.",
    "The capital of France is Paris, and about two million people live there.",
    "Sure! Here is a Python function that reverses a linked list:\n\ndef reverse(head):\n    prev = None",
    "I think the incumbent will win the election, given the current polling.",
    "For a sore throat, gargle warm salt water and rest; see a doctor if it lasts a week.",
    "An old silent pond / a frog jumps into the pond / splash, silence again.",
]

# (what the user wrote, the limiter types those words state)
STATED = [
    ("Add rate limiting of 60 requests per minute per user.", {"rpm"}),
    ("Throttle each API key to 100 calls a minute.", {"rpm"}),
    ("No key should be able to send more than 30 requests in any minute.", {"rpm"}),
    ("Limit llama-3.1-8b to 50000 tokens per minute.", {"tpm"}),
    ("Cap each model at 2 million tokens/min.", {"tpm"}),
    ("Meter by token usage: 100k per user each minute.", {"tpm"}),
    ("Limit both the requests and the tokens each user can use per minute.", {"rpm", "tpm"}),
    ("Add rate limiting on the API.", set()),
    ("Limit each user to 60 per minute.", set()),
    ("Protect the gateway from abuse with some sensible limits.", set()),
]


def check_scope(ask: Callable, replies_that_pass: list[str], repeat: int) -> tuple[list[str], list[str]]:
    """(on-topic replies the guard replaced, off-topic replies it let through)."""
    from orchestration.scope_check import violates

    replaced = [r for r in replies_that_pass if any(violates(r, ask) for _ in range(repeat))]
    let_through = [r for r in OFF_TOPIC if not all(violates(r, ask) for _ in range(repeat))]
    return replaced, let_through


def check_stated(ask: Callable, repeat: int) -> list[tuple[str, set, list]]:
    """(text, expected, what was returned) for each sample judged wrong at least once."""
    from orchestration import needs
    from orchestration.knowledge import must_ask

    _, question, wordings = must_ask()["policy"]
    wrong = []
    for text, expected in STATED:
        seen = []
        for _ in range(repeat):
            needs._stated_memo.clear()
            seen.append(needs.stated_options(text, ("rpm", "tpm"), ask, about=question, wordings=wordings))
        if any(got is None or set(got) != expected for got in seen):
            wrong.append((text, expected, seen))
    return wrong


def recorded_replies(paths: list[Path]) -> list[str]:
    """Every distinct reply in run_scripts.py output: all of them answer
    real deployment requests."""
    replies: dict[str, None] = {}
    for path in paths:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            for reply in record.get("replies") or [record.get("reply")]:
                if reply:
                    replies[reply] = None
    return list(replies)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--runs", type=Path, nargs="*", default=[])
    parser.add_argument("--only", choices=("scope", "stated"), help="measure one judge")
    args = parser.parse_args()

    from orchestration.graph import _ask_unbound

    replaced = let_through = wrong = []
    if args.only != "stated":
        on_topic = [*ON_TOPIC, *recorded_replies(args.runs)]
        replaced, let_through = check_scope(_ask_unbound, on_topic, args.repeat)
        print(f"scope   on-topic replies kept: {len(on_topic) - len(replaced)}/{len(on_topic)}")
        for reply in replaced:
            print(f"          REPLACED: {reply[:140]!r}")
        print(f"scope   off-topic replies caught: {len(OFF_TOPIC) - len(let_through)}/{len(OFF_TOPIC)}")
        for reply in let_through:
            print(f"          LET THROUGH: {reply[:140]!r}")

    if args.only != "scope":
        wrong = check_stated(_ask_unbound, args.repeat)
        print(f"stated  right: {len(STATED) - len(wrong)}/{len(STATED)}")
        for text, expected, seen in wrong:
            print(f"          {text!r}: expected {sorted(expected)}, got {seen}")

    sys.exit(1 if replaced or let_through or wrong else 0)


if __name__ == "__main__":
    main()
