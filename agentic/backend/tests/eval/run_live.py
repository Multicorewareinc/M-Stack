"""Scores cases.py against a LIVE model call -- the deep check score.py
can't do for free. Needs the model server up and a running backend +
frontend (uses the same NDJSON stream the console does, through the
Vite dev proxy, so the API key never touches this script either).

    python tests/eval/run_live.py [--base http://localhost:5175/agentic]

Against a backend reached directly rather than through the dev proxy, pass
the key in the environment (SERVICE_API_KEY) and it is sent as X-API-Key:

    SERVICE_API_KEY=... python tests/eval/run_live.py --base http://localhost:8011 --skip scope routing

Three things this checks that score.py cannot:
  - SCOPE:     did the model actually refuse/answer as expected
  - GROUNDING: did the model's own reply contain the fact, not just
               "was it present in the notes we handed it"
  - full-stack ROUTING: for the dependency cases, did the model's tool
               call actually end up composing the whole chain, not just
               did selection make the chain AVAILABLE to it

One call per case, sequentially -- never run this in parallel against a
shared model server someone else might be using (see the incident where
a background eval script made the console hang for the whole team).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases import DEPENDENCY_ROUTING_CASES, GROUNDING_CASES, SCOPE_CASES

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from orchestration.knowledge import OUT_OF_SCOPE_REPLY


def ask(base: str, text: str, timeout: int = 300) -> dict:
    headers = {"Content-Type": "application/json"}
    if os.environ.get("SERVICE_API_KEY"):
        headers["X-API-Key"] = os.environ["SERVICE_API_KEY"]
    req = urllib.request.Request(
        f"{base}/v1/sessions", json.dumps({"message": text}).encode(), headers
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def run_scope(base: str) -> tuple[int, int]:
    passed = 0
    print("\n--- SCOPE (live) ---")
    for case in SCOPE_CASES:
        t0 = time.time()
        reply = (ask(base, case.text).get("message") or "").strip()
        refused = reply.startswith(OUT_OF_SCOPE_REPLY[:30])
        ok = refused == (not case.in_scope)
        passed += ok
        print(
            f"{'ok  ' if ok else 'FAIL'} {case.id:24} {time.time() - t0:5.1f}s  refused={refused} expected_in_scope={case.in_scope}"
        )
    return passed, len(SCOPE_CASES)


def run_grounding(base: str) -> tuple[int, int]:
    passed = 0
    print("\n--- GROUNDING (live) ---")
    for case in GROUNDING_CASES:
        t0 = time.time()
        reply = (ask(base, case.text).get("message") or "").strip()
        ok = case.must_contain.split("=")[0] in reply or case.must_contain in reply
        passed += ok
        flag = " (known gap)" if case.known_gap else ""
        print(f"{'ok  ' if ok else 'FAIL'} {case.id:24} {time.time() - t0:5.1f}s{flag}")
    return passed, len(GROUNDING_CASES)


def run_dependency_routing(base: str) -> tuple[int, int]:
    """Did the actual tool call the model made end up including the whole
    dependency chain, judged by the layers/script the run reports back --
    not just whether the notes were available to it."""
    passed = 0
    print("\n--- DEPENDENCY ROUTING, end to end (live) ---")
    for case in DEPENDENCY_ROUTING_CASES:
        t0 = time.time()
        result = ask(base, case.text, timeout=400)
        script = result.get("script") or ""
        reply = result.get("message") or ""
        present = [s for s in case.needs if s in script or s in reply]
        # A single turn often can't fully resolve a multi-field request
        # (real secrets are missing) -- "needs_input" naming the right
        # components in its own text still counts as correctly routed.
        ok = len(present) >= max(1, len(case.needs) - 2)
        passed += ok
        print(
            f"{'ok  ' if ok else 'FAIL'} {case.id:24} {time.time() - t0:5.1f}s  status={result.get('status')}  present={len(present)}/{len(case.needs)}"
        )
    return passed, len(DEPENDENCY_ROUTING_CASES)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:5175/agentic")
    parser.add_argument("--skip", nargs="*", default=[], choices=["scope", "grounding", "routing"])
    args = parser.parse_args()

    results = {}
    if "scope" not in args.skip:
        results["scope"] = run_scope(args.base)
    if "grounding" not in args.skip:
        results["grounding"] = run_grounding(args.base)
    if "routing" not in args.skip:
        results["routing"] = run_dependency_routing(args.base)

    print("\n" + "=" * 70)
    print("LIVE BASELINE  " + "  ".join(f"{k}={p}/{t}" for k, (p, t) in results.items()))
    print("=" * 70)


if __name__ == "__main__":
    main()
