"""Scores a ragas_collect.py file with RAGAS, using our own models as judge.

Runs in its own environment, since RAGAS pins library versions the agent
does not use:

    python -m venv .ragas && .ragas/bin/pip install -r tests/eval/requirements-ragas.txt
    .ragas/bin/python tests/eval/ragas_score.py run.jsonl --out run.scores.jsonl
    .ragas/bin/python tests/eval/ragas_score.py --summary off.scores.jsonl on.scores.jsonl

Metrics, each skipped for a row that lacks what it needs:

  context_precision    are the returned passages relevant, relevant ones first  (contexts, reference)
  context_recall       do the passages hold everything the reference answer says (contexts, reference)
  faithfulness         is every claim in the answer supported by the passages    (contexts, response)
  answer_relevancy     does the answer address the question                      (response)
  factual_correctness  does the answer agree with the reference answer           (response, reference)
  factual_correctness_recall
                       does the answer state what the reference says; extra
                       correct detail is not penalised                          (response, reference)

The judge is gpt-oss-120b and the embeddings Qwen3-Embedding, both on the
shared model server, with one call in flight at a time. Resumable: a
(question, metric) pair already scored in --out is not scored again; one
that ended in an error is, and the new result is appended after it.

    JUDGE_URL        required, e.g. http://<models-host>:30800/v1 (no default)
    JUDGE_MODEL      default gpt-oss-120b
    JUDGE_REASONING  default low (the scores match "medium" and it is faster)
    JUDGE_MAX_TOKENS default 4096 (RAGAS's own 1024 cut off the claim lists
                     of longer answers, which failed as IncompleteOutputException)
    JUDGE_TIMEOUT_SECONDS default 180 per request; without one, a request the
                     server never answered held a run for eight hours
    EMBED_URL        required, e.g. http://<models-host>:30801/v1 (no default)
    EMBED_MODEL      default qwen3-embedding-0.6b
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

METRICS = (
    "context_precision",
    "context_recall",
    "faithfulness",
    "answer_relevancy",
    "factual_correctness",
    "factual_correctness_recall",
)
NEEDS = {
    "context_precision": ("contexts", "reference"),
    "context_recall": ("contexts", "reference"),
    "faithfulness": ("contexts", "response"),
    "answer_relevancy": ("response",),
    "factual_correctness": ("response", "reference"),
    "factual_correctness_recall": ("response", "reference"),
}


def _require_url(name: str) -> str:
    """The model server is not public, so there is no default: the URL must
    come from the environment, and a missing one fails loudly rather than
    silently reaching for a hardcoded host."""
    value = os.environ.get(name)
    if not value:
        raise SystemExit(
            f"{name} is required (no default), e.g. {name}=http://<models-host>:PORT/v1"
        )
    return value


def build_metrics(names: list[str]) -> dict:
    from openai import AsyncOpenAI
    from ragas.embeddings import OpenAIEmbeddings
    from ragas.llms import llm_factory
    from ragas.metrics.collections import (
        AnswerRelevancy,
        ContextPrecision,
        ContextRecall,
        FactualCorrectness,
        Faithfulness,
    )

    llm = llm_factory(
        os.environ.get("JUDGE_MODEL", "gpt-oss-120b"),
        provider="openai",
        client=AsyncOpenAI(
            base_url=_require_url("JUDGE_URL"),
            api_key="none",
            timeout=float(os.environ.get("JUDGE_TIMEOUT_SECONDS", "180")),
        ),
        reasoning_effort=os.environ.get("JUDGE_REASONING", "low"),
        max_tokens=int(os.environ.get("JUDGE_MAX_TOKENS", "4096")),
    )
    embeddings = OpenAIEmbeddings(
        client=AsyncOpenAI(
            base_url=_require_url("EMBED_URL"),
            api_key="none",
            timeout=float(os.environ.get("JUDGE_TIMEOUT_SECONDS", "180")),
        ),
        model=os.environ.get("EMBED_MODEL", "qwen3-embedding-0.6b"),
    )
    built = {
        "context_precision": lambda: ContextPrecision(llm=llm),
        "context_recall": lambda: ContextRecall(llm=llm),
        "faithfulness": lambda: Faithfulness(llm=llm),
        "answer_relevancy": lambda: AnswerRelevancy(llm=llm, embeddings=embeddings),
        "factual_correctness": lambda: FactualCorrectness(llm=llm),
        # F1 counts every claim the reference lacks against the answer; our
        # references are one sentence and the agent's answers add correct
        # detail, so recall is the measure of whether the answer is right.
        "factual_correctness_recall": lambda: FactualCorrectness(
            llm=llm, mode="recall", name="factual_correctness_recall"
        ),
    }
    return {name: built[name]() for name in names}


def arguments(metric: str, row: dict) -> dict:
    q, contexts = row["question"], row.get("contexts")
    return {
        "context_precision": lambda: dict(
            user_input=q, reference=row["reference"], retrieved_contexts=contexts
        ),
        "context_recall": lambda: dict(
            user_input=q, retrieved_contexts=contexts, reference=row["reference"]
        ),
        "faithfulness": lambda: dict(
            user_input=q, response=row["response"], retrieved_contexts=contexts
        ),
        "answer_relevancy": lambda: dict(user_input=q, response=row["response"]),
        "factual_correctness": lambda: dict(response=row["response"], reference=row["reference"]),
        "factual_correctness_recall": lambda: dict(
            response=row["response"], reference=row["reference"]
        ),
    }[metric]()


async def score(rows: list[dict], names: list[str], out: Path) -> None:
    done = set()
    if out.exists():
        done = {
            (r["id"], r["metric"])
            for r in map(json.loads, out.read_text().splitlines())
            if r and r["value"] is not None
        }
    metrics = build_metrics(names)
    todo = [
        (row, name)
        for row in rows
        for name in names
        if (row["id"], name) not in done and all(row.get(k) for k in NEEDS[name])
    ]
    print(f"{len(done)} already scored, {len(todo)} to go (including earlier errors) -> {out}")
    with out.open("a") as f:
        for n, (row, name) in enumerate(todo, start=1):
            started = time.perf_counter()
            try:
                result = await metrics[name].ascore(**arguments(name, row))
                value, error = float(result.value), None
            # Recorded, not fatal: one bad judge reply must not stop hours of scoring.
            except Exception as e:
                value, error = None, f"{type(e).__name__}: {str(e)[:300]}"
            took = time.perf_counter() - started
            f.write(
                json.dumps(
                    {
                        "id": row["id"],
                        "metric": name,
                        "value": value,
                        "error": error,
                        "seconds": round(took, 1),
                    }
                )
                + "\n"
            )
            f.flush()
            shown = "error" if value is None else f"{value:.3f}"
            print(f"  [{n}/{len(todo)}] {row['id']:36} {name:20} {shown:>6} {took:5.1f}s")


def summary(paths: list[Path]) -> None:
    """One column per scores file, one row per metric: the mean over the
    questions scored, and how many were scored."""
    tables = {}
    for path in paths:
        # A retried pair appears twice; a score, once there is one, wins.
        latest: dict[tuple[str, str], float | None] = {}
        for r in map(json.loads, path.read_text().splitlines()):
            key = (r["id"], r["metric"])
            if r["value"] is not None or key not in latest:
                latest[key] = r["value"]
        by_metric: dict[str, list[float]] = {}
        errors: dict[str, int] = {}
        for (_id, metric), value in latest.items():
            if value is None:
                errors[metric] = errors.get(metric, 0) + 1
            else:
                by_metric.setdefault(metric, []).append(value)
        tables[path.stem.removesuffix(".scores")] = (by_metric, errors)
    labels = list(tables)
    print(f"{'metric':22}" + "".join(f"{label:>24}" for label in labels))
    for name in METRICS:
        cells = []
        for label in labels:
            values, errors = tables[label][0].get(name), tables[label][1].get(name, 0)
            if not values:
                cells.append(f"{'-':>24}")
                continue
            note = f" ({len(values)}" + (f", {errors} err)" if errors else ")")
            cells.append(f"{statistics.mean(values):>11.3f}{note:>13}")
        print(f"{name:22}" + "".join(cells))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", type=Path, nargs="+")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--metrics", default=",".join(METRICS))
    parser.add_argument("--summary", action="store_true", help="print a table of scores files")
    args = parser.parse_args()
    if args.summary:
        summary(args.inputs)
        return
    if len(args.inputs) != 1 or args.out is None:
        parser.error("scoring takes one input file and --out")
    names = [m for m in args.metrics.split(",") if m]
    unknown = set(names) - set(METRICS)
    if unknown:
        parser.error(f"unknown metrics: {sorted(unknown)}")
    rows = [json.loads(line) for line in args.inputs[0].read_text().splitlines() if line]
    asyncio.run(score(rows, names, args.out))


if __name__ == "__main__":
    main()
