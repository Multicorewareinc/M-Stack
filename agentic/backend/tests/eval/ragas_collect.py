"""Collects what RAGAS scores, one grounding question at a time: the
passages the RAG service returns and, optionally, the agent's answer.

    PYTHONPATH=src RAG_SERVICE_API_KEY=... SERVICE_API_KEY=... \\
        python tests/eval/ragas_collect.py --out run.jsonl \\
            [--rag-url http://localhost:8020] [--agent-url http://localhost:8000] \\
            [--no-contexts] [--no-answers]

One JSON line per question: id, question, reference, the capabilities the
agent would select, the passages as text, and the agent's reply. Scored by
ragas_score.py, which runs in its own environment.

Sequential, and resumable: questions already in --out are skipped, so an
interrupted run continues where it stopped. Never run it in parallel
against the shared model server.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

import httpx

logging.getLogger("httpx").setLevel(logging.WARNING)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases import GROUNDING_CASES  # noqa: E402
from orchestration.grounding import search_query  # noqa: E402
from orchestration.selection import select  # noqa: E402
from references import REFERENCES  # noqa: E402


def contexts_for(client: httpx.Client, question: str, capabilities: list[str]) -> dict:
    # The search the agent makes, so the passages scored are the ones it saw.
    response = client.post(
        "/v1/search",
        json={
            "query": search_query(question),
            "capabilities": capabilities,
            "notes_loaded": True,
        },
    )
    response.raise_for_status()
    body = response.json()
    return {
        "contexts": [f"{p['context']}\n{p['text']}" for p in body["passages"]],
        "sources": [f"{p['path']}:{p['start']}-{p['end']}" for p in body["passages"]],
        "reranked": body.get("reranked", False),
    }


def answer_for(client: httpx.Client, question: str) -> str:
    response = client.post("/v1/sessions", json={"message": question})
    response.raise_for_status()
    return response.json().get("message") or ""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--rag-url", default="http://localhost:8020")
    parser.add_argument("--agent-url", default="http://localhost:8000")
    parser.add_argument("--no-contexts", action="store_true", help="skip the RAG service")
    parser.add_argument("--no-answers", action="store_true", help="skip the agent")
    parser.add_argument("--limit", type=int, help="only the first N questions")
    args = parser.parse_args()

    rag_key = os.environ.get("RAG_SERVICE_API_KEY", "")
    agent_key = os.environ.get("SERVICE_API_KEY", "")
    rag = httpx.Client(
        base_url=args.rag_url,
        headers={"Authorization": f"Bearer {rag_key}"} if rag_key else {},
        timeout=60,
    )
    agent = httpx.Client(
        base_url=args.agent_url, headers={"X-API-Key": agent_key} if agent_key else {}, timeout=400
    )

    done = set()
    if args.out.exists():
        done = {json.loads(line)["id"] for line in args.out.read_text().splitlines() if line}
    todo = [c for c in GROUNDING_CASES[: args.limit] if c.id not in done]
    print(f"{len(done)} already collected, {len(todo)} to go -> {args.out}")

    with args.out.open("a") as out:
        for n, case in enumerate(todo, start=1):
            started = time.perf_counter()
            capabilities = select(case.text, [], budget=100_000)[0]
            row = {
                "id": case.id,
                "question": case.text,
                "reference": REFERENCES[case.id],
                "capabilities": capabilities,
            }
            if not args.no_contexts:
                row.update(contexts_for(rag, case.text, capabilities))
            if not args.no_answers:
                row["response"] = answer_for(agent, case.text)
            out.write(json.dumps(row) + "\n")
            out.flush()
            print(f"  [{n}/{len(todo)}] {case.id} {time.perf_counter() - started:.1f}s")


if __name__ == "__main__":
    main()
