"""Turns a run_scripts.py run into the samples ragas_script_score.py scores:
per case and run, the tool call behind the model's last accepted plan and
the call a correct plan makes, each flattened to one entry per field, the
passages RAG returned beside the ones the call depended on, and why the
call is wrong when it is. Runs that never reached the agent are left out:
an outage is not a wrong call.

    PYTHONPATH=src python tests/eval/ragas_script_prepare.py run.jsonl --out samples.jsonl

Both calls pass through what the agent does to a call before the tool sees
it (orchestration/needs.py's as_sent) and then the SDK's own types,
so every field carries its value or the SDK default. A field the model
changed without being asked counts against it; one it spelled out at its
default does not. Fields the whole-stack tool wires itself are dropped:
the model may put anything there and the script replaces it. RAGAS
compares only the fields the reference names, so a `components` entry
lists what the plan builds, and a component nobody asked for counts
against the call too.

An ask case has no reference call: it is right when no plan was accepted
and the reply asks for the missing value, and RAGAS doesn't score it.
"""

from __future__ import annotations

import argparse
import inspect
import json
import sys
from functools import cache
from pathlib import Path
from typing import Any, get_type_hints

from pydantic import TypeAdapter

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp_tools._script import dump
from orchestration.needs import as_sent
from case_sets import SCRIPT_CASES
from script_cases import FULL_STACK, tool_function

TOOL = FULL_STACK
# Filled in by the tool from the component they point at; any value the
# model gives is replaced in the script.
WIRED = frozenset(
    {
        "kubeconfig_path",
        "storage_class",
        "org_cp_internal_url",
        "cache_url",
        "event_backbone_url",
        "policy_endpoints",
        "tokenizer_url",
        "api_upstream",
        "gateway_upstream",
        "ingress_address",
        "billing_internal_url",
        "admin_cp_url",
        "s3_endpoint_url",
    }
)
# Parameters that take one component or a list of them.
LIST_PARAMS = frozenset({"policy", "controlplane", "portal", "route"})


@cache
def _types(tool: str = TOOL) -> dict[str, Any]:
    return get_type_hints(inspect.unwrap(tool_function(tool)))


def _dumped(model: Any) -> Any:
    """A validated spec's fields. Its namespace is where it actually
    deploys: left empty, a component goes to its type's default, so
    spelling that default out is the same plan."""
    if not hasattr(model, "model_dump"):
        return model
    fields = dump(model)
    if "namespace" in fields and hasattr(model, "resolved_namespace"):
        fields["namespace"] = model.resolved_namespace
    return fields


@cache
def _defaults(tool: str) -> dict[str, Any]:
    return {
        name: param.default
        for name, param in inspect.signature(inspect.unwrap(tool_function(tool))).parameters.items()
        if param.default is not inspect.Parameter.empty
    }


def _given(tool: str, args: dict) -> dict:
    """`args` without the arguments sent as their default: `"billing": null`
    is the same call as no billing at all, and the strict comparison would
    otherwise count it as a difference."""
    try:
        defaults = _defaults(tool)
    except LookupError:
        return args
    return {k: v for k, v in args.items() if not (k in defaults and v == defaults[k])}


def _typed(tool: str, name: str, value: Any) -> Any:
    """`value` as the SDK reads it, defaults filled in; as given when the
    SDK rejects it."""
    if not isinstance(value, (dict, list)) or name not in _types(tool):
        return value
    try:
        validated = TypeAdapter(_types(tool)[name]).validate_python(value)
    except ValueError:
        return value
    if isinstance(validated, list):
        return [_dumped(v) for v in validated]
    return _dumped(validated)


def _label(item: dict, index: int) -> str:
    # Name first: two routes share a type, never a name.
    for key in ("name", "type", "address"):
        if isinstance(item.get(key), str):
            return item[key]
    return str(index)


def _flatten(prefix: str, value: Any, out: dict[str, Any]) -> None:
    if isinstance(value, dict):
        for key, inner in value.items():
            _flatten(f"{prefix}.{key}", inner, out)
    elif isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
        for index, item in enumerate(value):
            _flatten(f"{prefix}[{_label(item, index)}]", item, out)
    else:
        out[prefix] = value


def _wired(key: str, inference: bool) -> bool:
    parts = key.split(".")
    if len(parts) == 1:
        return False
    # The gateway's upstream is the user's own server unless inference is
    # composed, in which case the tool points it at that.
    return parts[-1] in WIRED or (inference and parts[-1] == "upstream_url")


def plan_fields(args: dict) -> dict[str, Any]:
    """A build_full_stack_plan call as one entry per field, defaults filled
    in and wired fields dropped."""
    args = _given(TOOL, as_sent(tool_function(TOOL), TOOL, args))
    components = sorted(k for k, v in args.items() if isinstance(v, (dict, list)))
    out: dict[str, Any] = {"components": ",".join(components)}
    for name, value in args.items():
        if isinstance(value, dict) and name in LIST_PARAMS:
            value = [value]
        _flatten(name, _typed(TOOL, name, value), out)
    inference = "inference" in components
    return {k: v for k, v in out.items() if not _wired(k, inference)}


def call_fields(tool: str, args: dict) -> dict[str, Any]:
    """Any plan tool's call as one entry per field, defaults filled in. A
    single-resource tool's fields are all the user's: none is wired."""
    if tool == TOOL:
        return plan_fields(args)
    out: dict[str, Any] = {}
    try:
        args = as_sent(tool_function(tool), tool, args)
    except LookupError:
        pass  # a tool the agent doesn't have: compare the call as given
    for name, value in _given(tool, args).items():
        try:
            typed = _typed(tool, name, value)
        except LookupError:
            typed = value
        _flatten(name, typed, out)
    return out


def _component(key: str) -> str | None:
    """The component a flattened field belongs to; None for a plan-level one."""
    head, sep, _ = key.replace("[", ".").partition(".")
    return head if sep else None


def _components(fields: dict) -> set[str]:
    return set(filter(None, str(fields.get("components", "")).split(",")))


def failure(response: dict | None, reference: dict, *, shown: int = 3) -> str | None:
    """Why `response` differs from the correct call, or None when it doesn't."""
    if response is None:
        return "no valid plan"
    if response["name"] != reference["name"]:
        return f"wrong tool: {response['name']}"
    got, want = response["args"], reference["args"]
    built, needed = _components(got), _components(want)
    reasons = []
    if built - needed:
        reasons.append(f"extra component: {', '.join(sorted(built - needed))}")
    if needed - built:
        reasons.append(f"missing component: {', '.join(sorted(needed - built))}")
    # A whole-stack call's fields are compared within the components both
    # build; a single-resource call has no components entry, so all of them.
    compared = built & needed if "components" in want else None
    wrong = sorted(
        k
        for k in got.keys() | want.keys()
        if k != "components"
        and (compared is None or _component(k) is None or _component(k) in compared)
        and got.get(k) != want.get(k)
    )
    if wrong:
        values = [f"{k}={got.get(k)!r} (expected {want.get(k)!r})" for k in wrong[:shown]]
        more = f" and {len(wrong) - shown} more" if len(wrong) > shown else ""
        reasons.append(f"wrong value: {'; '.join(values)}{more}")
    return " | ".join(reasons) or None


def ask_failure(case, response: dict | None, reply: str | None) -> str | None:
    """Why an ask case's answer is wrong, or None when it asked."""
    if response is not None:
        return f"built instead of asking: {response['name']}"
    if not reply:
        return "no reply"
    if not any(word in reply.lower() for word in case.ask_for):
        return f"did not ask for the missing value (no {' / '.join(case.ask_for)} in the reply)"
    return None


def sample(case, record: dict) -> dict:
    accepted = [
        a
        for a in record.get("attempts") or []
        if a.get("tool", "").startswith("build_") and a.get("valid") is True
    ]
    final = accepted[-1] if accepted else None
    response = (
        {"name": final["tool"], "args": call_fields(final["tool"], final["args"])}
        if final
        else None
    )
    reference = (
        None if case.asks else {"name": case.tool, "args": call_fields(case.tool, case.expected)}
    )
    return {
        "id": case.id,
        "run": record.get("run", 1),
        "ask": case.asks,
        "held_out": case.held_out,
        "user_input": case.request,
        "response_tool_call": response,
        "reference_tool_call": reference,
        "failure": (
            ask_failure(case, response, record.get("reply"))
            if case.asks
            else failure(response, reference)
        ),
        "retrieved_context_ids": record.get("sources") or [],
        "reference_context_ids": list(case.reference_context_ids),
    }


def samples(lines: list[str]) -> tuple[list[dict], int]:
    """(samples in case and run order, runs left out because they never
    reached the agent). A later record of the same run replaces an earlier one."""
    records: dict[tuple[str, int], dict] = {}
    unreached = set()
    for line in lines:
        if not line.strip():
            continue
        record = json.loads(line)
        key = (record["id"], record.get("run", 1))
        if "error" in record:
            unreached.add(key)
        else:
            records[key] = record
    order = {c.id: n for n, c in enumerate(SCRIPT_CASES)}
    cases = {c.id: c for c in SCRIPT_CASES}
    keys = sorted((k for k in records if k[0] in cases), key=lambda k: (order[k[0]], k[1]))
    return [sample(cases[k[0]], records[k]) for k in keys], len(unreached - records.keys())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    out, unreached = samples(args.run.read_text().splitlines())
    args.out.write_text("".join(json.dumps(s) + "\n" for s in out))
    print(f"{len(out)} samples -> {args.out}")
    if unreached:
        print(f"{unreached} run(s) never reached the agent and are left out; rerun run_scripts.py")


if __name__ == "__main__":
    main()
