#!/usr/bin/env bash
# Runs the script-generation evaluation with retrieval off and on, and
# scores both, in one command.
#
#   bash tests/eval/run_all.sh            # every case, 3 runs each, off then on
#   bash tests/eval/run_all.sh score      # only score what is already recorded
#
#   OUT=dir        where runs and scores go (default: a dated folder under
#                  ~/.local/state/multistack/eval)
#   REPEAT=3       runs per case; a case is right only if every run is
#   ONLY="a b"     case ids to run instead of all of them
#   SETUPS="off on"
#   CASES=fresh:7  a generated set of requests for seed 7 (fresh_cases.py)
#                  instead of the set the agent was developed against. Use
#                  the same CASES to score. The requests are written to
#                  OUT/cases.txt.
#
# Starts its own agent on :8011 for each setup and stops it afterwards. The
# console's backend on :8000 is never touched. Resumable: run it again and
# recorded runs are skipped. One request at a time to the model server.
#
# Each agent runs the code as it is when that setup starts, hours apart, so
# leave the agent's source and the SDK alone until both are done. A change
# in between is detected and reported with the scores.
set -u

BACKEND="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="$BACKEND/../../.venv/bin/python"
RAGAS_PY="${RAGAS_PY:-$HOME/.venvs/multistack-ragas/bin/python}"
OUT="${OUT:-${XDG_STATE_HOME:-$HOME/.local/state}/multistack/eval/$(date +%F)}"
REPEAT="${REPEAT:-3}"
SETUPS="${SETUPS:-off on}"
PORT=8011
# The model server is not public, so there is no default: set MODELS to its
# base URL. Only the setups that start the server need it (see models_up);
# `run_all.sh score` scores already-recorded runs and does not.
MODELS="${MODELS:-}"
# Read by the runner and by every scorer (case_sets.py).
export EVAL_CASES="${CASES:-}"

mkdir -p "$OUT"
exec > >(tee -a "$OUT/run_all.log") 2>&1
say() { echo "$(date '+%F %T') $*"; }

case "$EVAL_CASES" in
  fresh*)
    seed="${EVAL_CASES#fresh}"
    ( cd "$BACKEND" && PYTHONPATH=src RAG_ENABLED= "$PY" tests/eval/fresh_cases.py ${seed#:} ) > "$OUT/cases.txt" ||
      { say "could not generate the cases for CASES=$EVAL_CASES"; exit 1; }
    ;;
esac

models_up() {
  [ -n "$MODELS" ] || { say "set MODELS to the model server base URL, e.g. http://host (no default)"; return 1; }
  curl -s -m 10 "$MODELS:30800/v1/models" | grep -q '"gpt-oss-120b"' || return 1
  [ "$1" = off ] && return 0
  curl -s -m 10 "$MODELS:30801/v1/models" | grep -q '"id"' &&
    curl -s -m 10 "$MODELS:30802/v1/models" | grep -q '"id"' &&
    curl -s -m 5 -o /dev/null http://localhost:8020/health
}

# One checksum over everything a run's result depends on: the agent, the
# cases and scorers, and the SDK the plans are built with.
code_sum() {
  ( cd "$BACKEND/../.." &&
      find agentic/backend/src agentic/backend/tests/eval multistack -type f \
        \( -name '*.py' -o -name '*.md' \) -not -path '*/__pycache__/*' -print0 2>/dev/null |
      sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1 )
}

AGENT_PID=""
stop_agent() {
  [ -n "$AGENT_PID" ] && kill "$AGENT_PID" 2>/dev/null
  AGENT_PID=""
  for _ in $(seq 1 20); do curl -s -m 1 -o /dev/null "http://localhost:$PORT/health" || return 0; sleep 0.5; done
}
trap stop_agent EXIT

start_agent() {
  local rag="$1"
  if curl -s -m 2 -o /dev/null "http://localhost:$PORT/health"; then
    say "something already answers on :$PORT; stop it first"
    return 1
  fi
  # Conversations in memory: an evaluation run keeps nothing.
  ( cd "$BACKEND" && RAG_ENABLED="$rag" AGENTIC_STATE_DB= PYTHONPATH=src \
      exec "$PY" -m uvicorn main:create_app --factory --port "$PORT" --app-dir src \
  ) > "$OUT/agent_$( [ -n "$rag" ] && echo on || echo off ).log" 2>&1 &
  AGENT_PID=$!
  for _ in $(seq 1 60); do
    curl -s -m 2 -o /dev/null "http://localhost:$PORT/health" && return 0
    sleep 0.5
  done
  say "the agent did not come up on :$PORT"
  return 1
}

run_setup() {
  local setup="$1" rag=""
  [ "$setup" = on ] && rag=true
  if ! models_up "$setup"; then
    say "$setup: the model server or the RAG service is not answering; nothing run"
    return 1
  fi
  start_agent "$rag" || return 1
  echo "$(code_sum) $setup $(date '+%F %T')" >> "$OUT/code.sha256"
  say "$setup: running (repeat $REPEAT${ONLY:+, only $ONLY}${EVAL_CASES:+, cases $EVAL_CASES})"
  # shellcheck disable=SC2086
  ( cd "$BACKEND" && SERVICE_API_KEY="$(grep '^SERVICE_API_KEY=' .env | cut -d= -f2-)" PYTHONPATH=src \
      "$PY" tests/eval/run_scripts.py --setup "$setup" --out "$OUT/rag_$setup.jsonl" \
      --agent-url "http://localhost:$PORT" --repeat "$REPEAT" ${ONLY:+--only $ONLY} )
  local status=$?
  stop_agent
  say "$setup: finished (exit $status)"
  return $status
}

score() {
  local runs=()
  for setup in $SETUPS; do [ -s "$OUT/rag_$setup.jsonl" ] && runs+=("$OUT/rag_$setup.jsonl"); done
  [ ${#runs[@]} -eq 0 ] && { say "nothing recorded to score"; return 1; }
  ( cd "$BACKEND" && PYTHONPATH=src RAG_ENABLED= "$PY" tests/eval/score_runs.py "${runs[@]}" ) | tee "$OUT/results.txt"
  if [ "$(cut -d' ' -f1 "$OUT/code.sha256" 2>/dev/null | sort -u | wc -l)" -gt 1 ]; then
    echo "
NOT COMPARABLE: the code changed between the recorded runs (code.sha256 lists
which setup started on which version). Run the changed setups again." | tee -a "$OUT/results.txt"
  fi
  ( cd "$BACKEND" && PYTHONPATH=src RAG_ENABLED= "$PY" tests/eval/score_scripts.py "${runs[@]}" ) > "$OUT/score_scripts.txt"
  if [ -x "$RAGAS_PY" ]; then
    local samples=()
    for run in "${runs[@]}"; do
      ( cd "$BACKEND" && PYTHONPATH=src RAG_ENABLED= "$PY" tests/eval/ragas_script_prepare.py "$run" --out "${run%.jsonl}.samples.jsonl" ) >/dev/null
      samples+=("${run%.jsonl}.samples.jsonl")
    done
    ( cd "$BACKEND" && "$RAGAS_PY" tests/eval/ragas_script_score.py "${samples[@]}" ) > "$OUT/ragas.txt" 2>&1
  fi
  say "scores in $OUT"
}

if [ "${1:-}" != score ]; then
  for setup in $SETUPS; do run_setup "$setup" || say "$setup: stopped early; run again to resume"; done
fi
score
