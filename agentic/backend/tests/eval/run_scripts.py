"""Runs the script-generation cases against a live agent and records, per
case and run, everything the scorers need. The cases are script_cases.py's,
or a generated set when EVAL_CASES names one (case_sets.py).

    SERVICE_API_KEY=... python tests/eval/run_scripts.py --setup on --out run.jsonl \\
        [--agent-url http://localhost:8011] [--repeat 3] [--only chain-gateway ...]

The comparison is three runs, the agent restarted for each:

    --setup off      RAG_ENABLED unset
    --setup on       RAG_ENABLED=1, RAG_SERVICE_URL the RAG service, its index
                     rebuilt from this checkout (the reference passages are
                     cut by this checkout's chunker)
    --setup perfect  RAG_ENABLED=1, RAG_SERVICE_URL http://127.0.0.1:8099,
                     with perfect_rag.py running

Uses the agent's streaming endpoint, the one the console uses, so each
record also carries the passages retrieval supplied and every plan attempt
with the reason any was rejected.

--repeat runs every case that many times, all cases once before any twice:
the model answers the same request differently from run to run, and a
case counts as right only if every run is.

--setup names how the agent was started -- off (RAG_ENABLED unset), on
(the real RAG service) or perfect (perfect_rag.py) -- and the run stops
at the first sign the agent isn't running that way, before it records
hours of the wrong setup: a passage in an off run, anything but the
case's own passages in a perfect one, or three on runs in a row with no
passages, which is RAG switched off or failing (the agent then answers
without it, silently). Those three are not recorded. Three runs in a row
that never reach the agent or the model stop it too: the server is down.

Sequential and resumable: runs already in --out are skipped, except those
that failed to reach the agent. Never run it in parallel against the
shared model server.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from case_sets import ALL_CASES
from script_cases import ScriptCase


def _turn(client: httpx.Client, session: str | None, message: str, record: dict) -> str | None:
    """Sends one message and folds what the stream carries into `record`.
    Returns the session to continue on."""
    url = f"/v1/sessions/{session}/messages/stream" if session else "/v1/sessions/stream"
    with client.stream("POST", url, json={"message": message}) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line.strip():
                continue
            event = json.loads(line)
            kind = event.get("type")
            if kind == "sources":
                # Read before the first model call, and again when a plan's
                # values are checked against service code: all of it.
                for s in event["sources"]:
                    source = f"{s['path']}:{s['start']}-{s['end']}"
                    if source not in record["sources"]:
                        record["sources"].append(source)
            elif kind == "tool_call":
                record["attempts"].append({"tool": event["tool"], "args": event["args"]})
            elif kind == "tool_result" and record["attempts"]:
                record["attempts"][-1].update(
                    valid=event.get("valid"),
                    error=event.get("error"),
                    looked_up=event.get("looked_up", False),
                    needs=event.get("needs"),
                )
            elif kind == "ask_user":
                # The turn paused on a question with options. It ends here:
                # no final event follows until the user answers.
                session = event.get("session_id") or session
                record["pauses"].append(
                    {"question": event.get("question"), "options": event.get("options") or []}
                )
                record["reply"] = event.get("question")
            elif kind == "final":
                # The final event is the session response: the reply is
                # its `message`.
                session = event.get("session_id") or session
                record["script"] = event.get("script")
                record["reply"] = event.get("message")
    record["replies"].append(record["reply"])
    return session


def run_case(client: httpx.Client, request: str, answers: tuple[str, ...] = ()) -> dict:
    """One case: the request, then one of `answers` each time a turn ends
    without a script -- on a pause or on a question -- until there is a
    script or nothing left to say."""
    record: dict = {
        "sources": [], "attempts": [], "script": None, "reply": None, "pauses": [], "replies": [],
    }
    started = time.monotonic()
    session = _turn(client, None, request, record)
    for answer in answers:
        if record["script"] or session is None:
            break
        session = _turn(client, session, answer, record)
    record["seconds"] = round(time.monotonic() - started, 1)
    return record


def pending(out: Path, repeat: int, only: list[str] | None = None) -> list[tuple[int, ScriptCase]]:
    """(run, case) still to do: every case's runs 1..repeat, run-major, less
    those --out already holds. A run that never reached the agent is redone."""
    done = set()
    if out.exists():
        for line in out.read_text().splitlines():
            if line.strip():
                record = json.loads(line)
                if "error" not in record:
                    done.add((record["id"], record.get("run", 1)))
    return [
        (run, case)
        for run in range(1, repeat + 1)
        for case in ALL_CASES
        if (not only or case.id in only) and (case.id, run) not in done
    ]


SETUPS = ("off", "on", "perfect")
# On runs in a row with no passages before the run stops.
NO_PASSAGE_LIMIT = 3
# Runs in a row that never reached the agent or the model before the run
# stops: past that the server is down, and every further request fails at
# once without telling anything.
UNREACHED_LIMIT = 3


def wrong_setup(setup: str, case: ScriptCase, sources: list[str]) -> str | None:
    """Why `sources` show the agent isn't running `setup`, or None."""
    if setup == "off" and sources:
        return "passages came back in a RAG-off run: restart the agent with RAG_ENABLED unset"
    if setup == "perfect" and sources != list(case.reference_context_ids):
        return (
            f"{case.id} got {sources or 'no passages'}, not its reference passages: start "
            "perfect_rag.py and point the agent's RAG_SERVICE_URL at it"
        )
    return None


class Recorder:
    """Appends runs to --out, holding back on-runs with no passages until
    one with passages shows RAG is working; the third in a row stops the
    run unrecorded."""

    def __init__(self, out: Path, setup: str):
        self.out, self.setup, self.held = out, setup, []

    def add(self, line: dict) -> str | None:
        if "error" in line or self.setup != "on":
            self._write([line])
            return None
        if not line.get("sources"):
            self.held.append(line)
            if len(self.held) >= NO_PASSAGE_LIMIT:
                self.held = []
                return (
                    f"{NO_PASSAGE_LIMIT} RAG-on runs in a row got no passages: RAG is off or "
                    "failing. Check the agent's RAG_ENABLED and RAG_SERVICE_URL and the RAG "
                    "service; those runs were not recorded"
                )
            return None
        self._write([*self.held, line])
        self.held = []
        return None

    def close(self) -> None:
        self._write(self.held)
        self.held = []

    def _write(self, lines: list[dict]) -> None:
        if lines:
            with self.out.open("a") as f:
                f.writelines(json.dumps(line) + "\n" for line in lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--setup", choices=SETUPS, required=True, help="how the agent was started")
    parser.add_argument("--agent-url", default="http://localhost:8011")
    parser.add_argument("--repeat", type=int, default=1, help="runs per case")
    parser.add_argument("--only", nargs="*", default=None, help="case ids to run")
    args = parser.parse_args()

    todo = pending(args.out, args.repeat, args.only)
    headers = (
        {"X-API-Key": os.environ["SERVICE_API_KEY"]} if os.environ.get("SERVICE_API_KEY") else {}
    )
    recorder = Recorder(args.out, args.setup)
    unreached = 0
    with httpx.Client(base_url=args.agent_url, headers=headers, timeout=600) as client:
        for n, (run, case) in enumerate(todo, start=1):
            try:
                record = run_case(client, case.request, case.answers)
            except httpx.HTTPError as e:
                record = {"error": str(e)}
            unreached = unreached + 1 if "error" in record else 0
            problem = (
                None if "error" in record else wrong_setup(args.setup, case, record["sources"])
            )
            if problem:
                sys.exit(f"stopped, {case.id} run {run} not recorded: {problem}")
            problem = recorder.add({"id": case.id, "run": run, "setup": args.setup, **record})
            attempts = record.get("attempts", [])
            print(
                f"[{n}/{len(todo)}] {case.id} run {run}: "
                f"script={'yes' if record.get('script') else 'no'} "
                f"attempts={len(attempts)} rejected={sum(1 for a in attempts if a.get('valid') is False)} "
                f"pauses={len(record.get('pauses') or [])} "
                f"passages={len(record.get('sources') or [])} {record.get('seconds', '?')}s",
                flush=True,
            )
            if problem:
                sys.exit(f"stopped: {problem}")
            if unreached >= UNREACHED_LIMIT:
                recorder.close()
                sys.exit(
                    f"stopped: {UNREACHED_LIMIT} runs in a row never reached the agent or the "
                    "model server; they are redone when the run resumes"
                )
    recorder.close()


if __name__ == "__main__":
    main()
