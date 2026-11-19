"""Scores ragas_script_prepare.py samples with RAGAS. Neither metric uses a
judge model, so no model server is needed to score.

    .ragas/bin/python tests/eval/ragas_script_score.py rag_off.jsonl rag_on.jsonl perfect_rag.jsonl \\
        [--json verdicts.json]

  tool_call_accuracy   did the plan the model ended on call the right tool
                       with the right value in every field
                       (ToolCallAccuracy); 1.0 is the whole call right.
                       It averages over every field, so one wrong field
                       in a 67-field call still scores 0.99 -- and makes
                       a wrong script. Each run is right or wrong, and a
                       case is right only if every run of it is.
  tool_call_f1         the whole call compared as one, every field of it
                       including any the reference lacks (ToolCallF1): 1.0
                       when it is exactly the reference call, else 0.0. A
                       run is right only when both this and
                       tool_call_accuracy are 1.0.
  context_recall       did RAG return the passage the call depended on
                       (IDBasedContextRecall); a case with no such passage
                       is not scored for it

An ask case has no call to compare: it is right when nothing was built and
the reply asks for the missing value (ragas_script_prepare.py decides), and
it takes no part in tool_call_accuracy. One with a reference passage is
still scored for retrieval.

Several files side by side compare setups. The first is the baseline: each
later one lists the cases it fixed and broke against it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import warnings
from dataclasses import asdict
from pathlib import Path

from ragas.dataset_schema import SingleTurnSample
from ragas.messages import AIMessage, HumanMessage, ToolCall

# This version's collections API has no ID-based recall; the older metric
# is the one it has.
from ragas.metrics import _IDBasedContextRecall as IDBasedContextRecall
from ragas.metrics.collections import ToolCallAccuracy, ToolCallF1

sys.path.insert(0, str(Path(__file__).resolve().parent))

from script_report import fixed_and_broken, verdicts

# A case whose plan was never accepted has no tool call to compare; RAGAS
# warns and scores it 0, which is the score it should get.
warnings.filterwarnings("ignore", message="No tool calls found")


def _call(call: dict) -> ToolCall:
    return ToolCall(name=call["name"], args=call["args"])


async def _recall(sample: dict) -> float | None:
    if not sample["reference_context_ids"]:
        return None
    return await IDBasedContextRecall().single_turn_ascore(
        SingleTurnSample(
            retrieved_context_ids=sample["retrieved_context_ids"],
            reference_context_ids=sample["reference_context_ids"],
        )
    )


async def score(sample: dict) -> dict:
    if sample.get("ask"):
        return {
            "id": sample["id"],
            "run": sample.get("run", 1),
            "accuracy": None,
            "f1": None,
            "right": sample["failure"] is None,
            "recall": await _recall(sample),
            "failure": sample["failure"],
        }
    messages = [HumanMessage(content=sample["user_input"])]
    if sample["response_tool_call"]:
        messages.append(AIMessage(content="", tool_calls=[_call(sample["response_tool_call"])]))
    reference = [_call(sample["reference_tool_call"])]
    accuracy = await ToolCallAccuracy().ascore(user_input=messages, reference_tool_calls=reference)
    f1 = await ToolCallF1().ascore(user_input=messages, reference_tool_calls=reference)
    recall = await _recall(sample)
    return {
        "id": sample["id"],
        "run": sample.get("run", 1),
        "accuracy": accuracy.value,
        "f1": f1.value,
        "right": accuracy.value == 1.0 and f1.value == 1.0,
        "recall": recall,
        "failure": sample.get("failure"),
    }


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else float("nan")


def summary(results: list[dict], v: dict, kinds: dict) -> dict:
    def right(ids) -> dict:
        ids = [i for i in ids if i in v]
        return {"right": sum(1 for i in ids if v[i].right), "of": len(ids)}

    with_passage = [c for c in v.values() if c.found is not None]
    accuracies = [r["accuracy"] for r in results if r["accuracy"] is not None]
    f1s = [r["f1"] for r in results if r.get("f1") is not None]
    recalls = [r["recall"] for r in results if r["recall"] is not None]
    return {
        "cases": right(list(v)),
        "build": right([i for i in v if not kinds[i].get("ask")]),
        "ask": right([i for i in v if kinds[i].get("ask")]),
        "held_out": right([i for i in v if kinds[i].get("held_out")]),
        "runs": {"right": sum(1 for r in results if r["right"]), "of": len(results)},
        "found": {"right": sum(1 for c in with_passage if c.found), "of": len(with_passage)},
        "tool_call_accuracy": _mean(accuracies),
        "tool_call_f1": _mean(f1s),
        "context_recall": _mean(recalls),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("samples", nargs="+", type=Path)
    parser.add_argument("--json", type=Path, help="also write the verdicts here")
    args = parser.parse_args()
    runs = [(p.stem, load(p)) for p in args.samples]
    scored = {name: [await score(s) for s in samples] for name, samples in runs}
    judged = {name: verdicts(results) for name, results in scored.items()}
    ids = list(dict.fromkeys(case for v in judged.values() for case in v))

    print(f"{'case':28}" + "".join(f"{name:>26}" for name in judged))
    for case_id in ids:
        cells = []
        for name in judged:
            v = judged[name].get(case_id)
            if v is None:
                cells.append(f"{'(not run)':>26}")
                continue
            call = f"{'right' if v.right else 'WRONG'} {v.right_runs}/{v.runs}"
            found = "-" if v.found_runs is None else f"{v.found_runs}/{v.runs}"
            cells.append(f"{call:9} found {found:5}".rjust(26))
        print(f"{case_id:28}" + "".join(cells))

    kinds = {s["id"]: s for _, samples in runs for s in samples}
    summaries = {n: summary(scored[n], judged[n], kinds) for n in judged}

    def of(count: dict) -> str:
        return f"{count['right']}/{count['of']}"

    print()
    for name, s in summaries.items():
        # Four places: one wrong field in one run of thirty still averages
        # to 0.9995, which three would round to a perfect score.
        print(
            f"{name}: {of(s['cases'])} cases right in every run ({of(s['runs'])} runs right, "
            f"tool_call_accuracy {s['tool_call_accuracy']:.4f}, "
            f"tool_call_f1 {s['tool_call_f1']:.4f})  "
            f"RAG found the passage in every run {of(s['found'])} "
            f"(context_recall {s['context_recall']:.3f})"
        )
        print(
            f"  build cases {of(s['build'])}  ask cases {of(s['ask'])}  "
            f"held out {of(s['held_out'])}"
        )

    for name, v in judged.items():
        wrong = [(case_id, f) for case_id, c in v.items() for f in c.failures]
        if wrong:
            print(f"\n{name} -- why runs were wrong:")
            for case_id, f in wrong:
                print(f"  {case_id:28}{f}")

    (base, base_v), *others = judged.items()
    against = {}
    for name, v in others:
        fixed, broken = fixed_and_broken(base_v, v)
        against[name] = {"base": base, "fixed": fixed, "broken": broken}
        print(
            f"\n{name} against {base}: fixed {len(fixed)} {fixed or ''}  "
            f"broke {len(broken)} {broken or ''}"
        )

    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "setups": list(judged),
                    "summary": summaries,
                    "cases": {
                        n: {case_id: asdict(c) for case_id, c in v.items()}
                        for n, v in judged.items()
                    },
                    "against": against,
                },
                indent=1,
            )
        )
        print(f"\nverdicts -> {args.json}")


if __name__ == "__main__":
    asyncio.run(main())
