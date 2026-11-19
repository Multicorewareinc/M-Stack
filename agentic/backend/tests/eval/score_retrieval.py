"""Scores the RAG service against the grounding cases, over its HTTP API.

    RAG_SERVICE_URL=http://localhost:8020 RAG_SERVICE_API_KEY=... \\
        PYTHONPATH=src python tests/eval/score_retrieval.py [--detail]

A case is a hit when a returned passage contains the expected fact and
belongs to the capability the question is about. Reports where the first
hit sits among the passages the service returns, and how the service's
relevance gate treats questions about the platform versus ones that are not.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import httpx

logging.getLogger("httpx").setLevel(logging.WARNING)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases import GROUNDING_CASES, SCOPE_CASES  # noqa: E402
from orchestration.selection import select  # noqa: E402

# Questions with nothing to do with the platform, and bare greetings.
NOISE = [
    "what is the capital of france",
    "explain quantum computing in simple terms",
    "how do I bake sourdough bread",
    "what's the weather like in chennai",
    "write a poem about the sea",
    "who won the world cup in 2022",
    "how do I set up a django project",
    "translate good morning into spanish",
    "hi",
    "thanks!",
    "hello there",
]


def search(client: httpx.Client, question: str, capabilities=()) -> dict:
    response = client.post(
        "/v1/search", json={"query": question, "capabilities": list(capabilities)}
    )
    response.raise_for_status()
    return response.json()


def is_hit(passage: dict, case) -> bool:
    return case.must_contain in passage["text"] and passage["capability"] in case.skills


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--detail", action="store_true", help="print each case's rank")
    args = parser.parse_args()
    url = os.environ.get("RAG_SERVICE_URL", "http://localhost:8020")
    key = os.environ.get("RAG_SERVICE_API_KEY")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    client = httpx.Client(base_url=url, headers=headers, timeout=60)

    total = len(GROUNDING_CASES)
    print(f"{total} grounding cases against {url}\n")
    ranks: list[int | None] = []
    gated_out: list[str] = []
    for case in GROUNDING_CASES:
        result = search(client, case.text, select(case.text, [], budget=100_000)[0])
        if result["skipped"]:
            gated_out.append(case.id)
        rank = next(
            (n for n, p in enumerate(result["passages"], start=1) if is_hit(p, case)), None
        )
        ranks.append(rank)
        if args.detail:
            label = case.id + (" *" if case.known_gap else "")
            print(f"{label:40} {'-' if rank is None else rank}")

    found = sum(r is not None for r in ranks)
    gaps = [i for i, c in enumerate(GROUNDING_CASES) if c.known_gap]
    mrr = sum(1 / r for r in ranks if r) / total
    print(f"fact among the passages sent to the model: {found}/{total}")
    print(f"  top-1 {sum(r == 1 for r in ranks)}/{total}, MRR {mrr:.2f}")
    print(
        f"  of the {len(gaps)} facts only the code can answer: "
        f"{sum(ranks[i] is not None for i in gaps)}/{len(gaps)}"
    )
    print(f"  questions about the platform the gate turned away: {len(gated_out)} {gated_out}")

    def added_nothing(question: str) -> bool:
        return not search(client, question)["passages"]

    scope_in = [c.text for c in SCOPE_CASES if c.in_scope]
    scope_out = [c.text for c in SCOPE_CASES if not c.in_scope]
    print("\nwhat the gate lets through (nothing added = correct for the last two):")
    print(f"  in-scope scope cases    {sum(not added_nothing(q) for q in scope_in)}/{len(scope_in)}")
    print(f"  out-of-scope questions  {sum(added_nothing(q) for q in scope_out)}/{len(scope_out)}")
    print(f"  unrelated and greetings {sum(added_nothing(q) for q in NOISE)}/{len(NOISE)}")


if __name__ == "__main__":
    main()
