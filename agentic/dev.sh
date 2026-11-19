#!/usr/bin/env bash
# Starts, stops or reports the local console stack: the search database
# (Qdrant), the RAG service, the agent backend and the console.
#
#   agentic/dev.sh start | stop | status
#
# A part that already answers on its port is left alone, so `start` is
# safe to run at any time, for example after a reboot. Nothing here calls
# the model server; each service only does that when it is used.
#
# Qdrant's binary and data live in ~/qdrant (QDRANT_DIR to change it), as
# set up in api/microservices/rag/README.md. Logs go to
# ~/.local/state/multistack/.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
LOGS="${XDG_STATE_HOME:-$HOME/.local/state}/multistack"
QDRANT_DIR="${QDRANT_DIR:-$HOME/qdrant}"

# name | port | url that answers once it is up
PARTS=(
  "search database|6333|http://localhost:6333/collections"
  "RAG service|8020|http://localhost:8020/health"
  "agent backend|8000|http://localhost:8000/health"
  "console|5175|http://localhost:5175/"
)

up() { curl -s -m 2 -o /dev/null "$1"; }

wait_for() {
  local name="$1" url="$2"
  for _ in $(seq 1 60); do
    up "$url" && { echo "  $name: up"; return 0; }
    sleep 0.5
  done
  echo "  $name: did not come up, see $LOGS"
  return 1
}

# Detached from this shell, so closing the terminal doesn't stop it. The
# service replaces the subshell and takes none of this script's output
# with it, so `dev.sh start | tee log` ends when the script does.
launch() {
  local log="$1" dir="$2"
  shift 2
  ( cd "$dir" && exec setsid nohup "$@" ) > "$LOGS/$log.log" 2>&1 < /dev/null &
}

start() {
  mkdir -p "$LOGS"
  if ! up http://localhost:6333/collections; then
    if [ ! -x "$QDRANT_DIR/qdrant" ]; then
      echo "  search database: no Qdrant binary at $QDRANT_DIR/qdrant (api/microservices/rag/README.md, one-time setup)"
      return 1
    fi
    launch qdrant "$QDRANT_DIR" env QDRANT__TELEMETRY_DISABLED=true ./qdrant
  fi
  wait_for "search database :6333" http://localhost:6333/collections || return 1

  up http://localhost:8020/health ||
    launch rag "$ROOT/api/microservices/rag/src" "$PY" -m uvicorn main:create_app --factory --port 8020
  wait_for "RAG service :8020" http://localhost:8020/health || return 1

  up http://localhost:8000/health ||
    launch agent "$ROOT/agentic/backend" env PYTHONPATH=src "$PY" -m uvicorn main:create_app --factory --port 8000 --app-dir src
  wait_for "agent backend :8000" http://localhost:8000/health || return 1

  up http://localhost:5175/ || launch console "$ROOT/agentic/frontend" npm run dev
  wait_for "console :5175" http://localhost:5175/ || return 1
  echo "open http://localhost:5175"
}

listening_on() { ss -ltnpH "sport = :$1" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u; }

stop() {
  local part name port pid
  for part in "${PARTS[@]}"; do
    IFS='|' read -r name port _ <<< "$part"
    for pid in $(listening_on "$port"); do
      kill "$pid" 2>/dev/null && echo "  $name :$port: stopped"
    done
  done
}

status() {
  local part name port url
  for part in "${PARTS[@]}"; do
    IFS='|' read -r name port url <<< "$part"
    if up "$url"; then echo "  $name :$port: up"; else echo "  $name :$port: down"; fi
  done
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  *) echo "usage: $0 start | stop | status"; exit 2 ;;
esac
