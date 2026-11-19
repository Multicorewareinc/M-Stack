"""A stand-in for the RAG service that answers each script case's request
with exactly the passages it needs (script_cases.py's reference ids): the
perfect-RAG setup, where retrieval is never wrong. A case that still fails
here fails because of the model or the prompt, not the search.

    PYTHONPATH=src python tests/eval/perfect_rag.py [--port 8099]

then start the agent with RAG_ENABLED=1 and
RAG_SERVICE_URL=http://127.0.0.1:8099, and run run_scripts.py as usual.

The passages are cut by RAG's own chunker and rendered by its own prompt
module, so the model reads them exactly as it would from the real service.
Any other search -- a case with no reference passage, the lookup after a
rejected plan -- finds nothing, as with RAG off.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(Path(__file__).resolve().parent))
# Appended, not prepended: the RAG service shares module names with the
# agent (settings, service, ...), and only chunking, corpus and prompt are
# wanted from it.
sys.path.append(str(REPO_ROOT / "api/microservices/rag/src"))

import prompt  # noqa: E402
from corpus import chunk_file  # noqa: E402
from orchestration.grounding import search_query  # noqa: E402
from script_cases import SCRIPT_CASES  # noqa: E402


@dataclass(frozen=True)
class Passage:
    path: str
    start: int
    end: int
    context: str
    kind: str
    capability: str | None
    text: str


def passage(ref: str) -> Passage:
    """The chunk RAG indexes as `ref` (path:start-end)."""
    path, span = ref.rsplit(":", 1)
    start, end = (int(n) for n in span.split("-"))
    for chunk in chunk_file(REPO_ROOT / path, repo_root=REPO_ROOT):
        if (chunk.start_line, chunk.end_line) == (start, end):
            return Passage(
                path, start, end, chunk.context, chunk.kind, chunk.capability, chunk.text
            )
    raise LookupError(f"{ref}: the chunker no longer cuts this passage; update script_cases.py")


def answers() -> dict[str, list[Passage]]:
    """What each case's request searches with (the agent cleans it first),
    mapped to the case's reference passages."""
    return {
        search_query(c.request): [passage(r) for r in c.reference_context_ids]
        for c in SCRIPT_CASES
        if c.reference_context_ids
    }


def search(query: str, table: dict[str, list[Passage]]) -> dict:
    """A /v1/search response, shaped as the RAG service's."""
    found = table.get(query, [])
    return {
        "passages": [{**asdict(p), "score": 1.0, "rerank_score": None} for p in found],
        "rendered": prompt.render(found),
        "best_similarity": 1.0 if found else 0.0,
        "skipped": None if found else "perfect RAG: no reference passage for this query",
        "reranked": False,
        "llm_reranked": False,
    }


def server(port: int, table: dict[str, list[Passage]]) -> HTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/v1/status":
                self._send({"chunks": sum(len(p) for p in table.values()), "commit": None})
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            if self.path != "/v1/search":
                self.send_error(404)
                return
            query = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["query"]
            body = search(query, table)
            print(f"{len(body['passages'])} passage(s) for: {query[:80]}", flush=True)
            self._send(body)

        def log_message(self, *args) -> None:
            pass

    return HTTPServer(("127.0.0.1", port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()
    table = answers()
    httpd = server(args.port, table)
    print(f"perfect RAG on http://127.0.0.1:{args.port} for {len(table)} cases", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
