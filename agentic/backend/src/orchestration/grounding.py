"""Reference lookups for the agent, from the RAG service
(api/microservices/rag), each returning the text to show the model plus a
list of sources for the console:

- field_docs(): the SDK fields of the components a request builds, read
  by name -- what each field means, its default, its limits. The tool
  schema has the field names and types but none of that.
- value_uses(): for values a plan sets, the service code that works with
  that field -- where a service may accept fewer values than the SDK does.
- retrieve(): a search by meaning, used after the SDK rejects a plan.
- relevant_skills(): every skill ranked by meaning against a request --
  what selection.py's select_skills() calls instead of a judgement call.
- dependencies_for(): each capability's direct dependency edges, read by
  name -- what selection.py's chain() walks breadth-first for the full
  dependency chain, instead of a DEPENDS_ON dict lookup.
- tool_for(): which MCP tool builds a capability on its own, read by
  name -- what selection.py's _tools_for() looks up, instead of a
  TOOL_FOR_SKILL dict lookup.

Off unless RAG_ENABLED is set. Whether a plan is valid still stays decided
by the SDK, never by anything in this module -- retrieval only changes
what the model reads and which skills/dependencies/tools selection.py
resolves a request to. Every failure -- service unset, unreachable, slow,
or answering with an error -- returns nothing, so a request is never worse
off than without retrieval.

The index is built from the source at one commit and does not follow later
edits. On first use this logs which commit it came from, and, when running
from a git checkout, warns if the indexed source has changed since.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Above the RAG service's own worst case: embedding the question (4s),
# reranking (5s) and the chat model's ranking (15s), each of which it gives
# up on and answers without.
DEFAULT_TIMEOUT_SECONDS = 25.0

# A lookup after a rejection is about one field, read beside the turn's own
# passages: fewer of them, in less room.
REJECTION_TOP_K = 4
REJECTION_BUDGET_TOKENS = 2000

# What a search needs from a request is its words. Addresses and file paths
# find no code, and a credential the user typed must not leave the agent.
_URL = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://\S+")
_PATH = re.compile(r"(?<![\w.:/-])/(?:[\w.-]+/)+[\w.-]*")
_PASSWORD = re.compile(r"(?i)\b(password|passwd|passphrase)\b(?:\s+is|\s*[:=])?\s+\S+")
_TOKEN = re.compile(r"(?i)\b(token|api key|access key|secret key)\b(?:\s+is|\s*[:=])\s*\S+")

REPO_ROOT = Path(__file__).resolve().parents[4]
# What the RAG service indexes (its records.INDEXED_PATHS); a change
# anywhere else can't make the index stale.
INDEXED_PATHS = ("multistack", "api/microservices", "agentic/backend/src/skills")

_client: httpx.Client | None = None
_index_checked = False


def enabled() -> bool:
    return os.environ.get("RAG_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def _get_client() -> httpx.Client | None:
    global _client
    if _client is None:
        url = os.environ.get("RAG_SERVICE_URL", "").strip()
        if not url:
            logger.warning("retrieval: RAG_ENABLED is set but RAG_SERVICE_URL is not; skipping")
            return None
        key = os.environ.get("RAG_SERVICE_API_KEY", "")
        _client = httpx.Client(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {key}"} if key else {},
            timeout=float(os.environ.get("RAG_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)),
        )
    return _client


def reset() -> None:
    """Forgets the client; the next call builds it from the environment again."""
    global _client, _index_checked
    if _client is not None:
        _client.close()
    _client = None
    _index_checked = False


def _indexed_source_changed_since(commit: str) -> bool | None:
    """Whether the indexed source differs between `commit` and the working
    tree, new untracked files included. None when that can't be told: no
    git, not a checkout, or a commit this checkout doesn't have."""
    try:
        diff = subprocess.run(
            ["git", "diff", "--quiet", commit, "--", *INDEXED_PATHS],
            cwd=REPO_ROOT,
            capture_output=True,
            timeout=5,
        )
        if diff.returncode not in (0, 1):
            return None
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "--", *INDEXED_PATHS],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return diff.returncode == 1 or bool(untracked.stdout.strip())


def _check_index(client: httpx.Client) -> None:
    """Once per process: which commit the index was built from, and a
    warning if the indexed source has changed since. Never raises."""
    global _index_checked
    if _index_checked:
        return
    _index_checked = True
    try:
        response = client.get("/v1/status")
        response.raise_for_status()
        status = response.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.info("retrieval: index status unavailable: %s", e)
        return
    commit = status.get("commit")
    logger.info(
        "retrieval: index of %s chunks, built %s from commit %s",
        status.get("chunks"),
        status.get("built_at"),
        commit or "unknown",
    )
    if status.get("commit_modified"):
        # Built from uncommitted edits: its commit alone doesn't say what's
        # in it, so a comparison against the checkout would mislead.
        logger.info("retrieval: the index includes changes not committed at %s", commit)
        return
    if commit and _indexed_source_changed_since(commit):
        logger.warning(
            "retrieval: the SDK, service source or skill notes have changed since commit %s, "
            "which the index was built from -- re-run the RAG indexer",
            commit,
        )


def search_query(message: str) -> str:
    """`message` without its addresses, file paths and credentials. Secret
    names stay: a name is not a credential, and it names what it holds."""
    text = _PASSWORD.sub(r"\1", message)
    text = _TOKEN.sub(r"\1", text)
    text = _URL.sub("", text)
    text = _PATH.sub("", text)
    return " ".join(text.split())


def _post(path: str, body: dict) -> dict | None:
    """The service's answer, or None when retrieval is off, unavailable or
    fails -- a request is never worse off than without it."""
    if not enabled():
        return None
    client = _get_client()
    if client is None:
        return None
    _check_index(client)
    try:
        response = client.post(path, json=body)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("retrieval: %s skipped: %s", path, e)
        return None


def _sources(passages: list[dict]) -> list[dict]:
    return [{k: v for k, v in p.items() if k != "text"} for p in passages]


def field_docs(
    capabilities: Sequence[str], *, budget_tokens: int | None = None
) -> tuple[str, list[dict]]:
    """(text for the prompt, sources) for the SDK fields of `capabilities`,
    the ones the request names first."""
    if not capabilities:
        return "", []
    body: dict = {"capabilities": list(capabilities)}
    if budget_tokens is not None:
        body["budget_tokens"] = budget_tokens
    found = _post("/v1/fields", body)
    if not found:
        return "", []
    try:
        rendered, sources = found["rendered"], _sources(found["passages"])
    except (KeyError, TypeError) as e:
        logger.warning("retrieval: /v1/fields answer unreadable: %s", e)
        return "", []
    logger.info("retrieval: %d field passage(s) for %s", len(sources), ", ".join(capabilities))
    return rendered, sources


def value_uses(items: Sequence[tuple[str, str, str]]) -> list[dict]:
    """For each (capability, field, value): {"capability", "name", "value",
    "passages"} -- the service code that works with that field, with text.
    Empty when there is nothing to look up or retrieval is off."""
    if not items:
        return []
    found = _post(
        "/v1/uses",
        {"items": [{"capability": c, "name": n, "value": v} for c, n, v in items]},
    )
    try:
        return list(found["uses"]) if found else []
    except (KeyError, TypeError) as e:
        logger.warning("retrieval: /v1/uses answer unreadable: %s", e)
        return []


def retrieve_for_rejection(error: str, skills: Sequence[str]) -> str:
    """Passages bearing on why the SDK rejected a plan, for the retry. The
    error names the field or value that failed, which the request the turn
    searched with may never mention."""
    rendered, _ = retrieve(
        error, skills, top_k=REJECTION_TOP_K, budget_tokens=REJECTION_BUDGET_TOKENS
    )
    return rendered


def retrieve(
    message: str,
    skills: Sequence[str],
    *,
    top_k: int | None = None,
    budget_tokens: int | None = None,
) -> tuple[str, list[dict]]:
    """(text for the prompt, sources for the console), both empty when
    retrieval is off, unavailable, or finds nothing relevant. `skills` are
    the capabilities the request is about, used to favour their chunks;
    their skill notes are already in the system prompt, so the search
    leaves them out."""
    if not enabled():
        return "", []
    query = search_query(message)
    if not query:
        return "", []
    client = _get_client()
    if client is None:
        return "", []
    _check_index(client)
    body: dict = {"query": query, "capabilities": list(skills), "notes_loaded": True}
    if top_k is not None:
        body["top_k"] = top_k
    if budget_tokens is not None:
        body["budget_tokens"] = budget_tokens
    try:
        response = client.post("/v1/search", json=body)
        response.raise_for_status()
        body = response.json()
        rendered = body["rendered"]
        sources = [{k: v for k, v in p.items() if k != "text"} for p in body["passages"]]
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
        logger.warning("retrieval: skipped for this message: %s", e)
        return "", []
    logger.info(
        "retrieval: %d passage(s), best similarity %.2f%s",
        len(sources),
        body.get("best_similarity") or 0.0,
        f" (skipped: {body['skipped']})" if body.get("skipped") else "",
    )
    return rendered, sources


def relevant_skills(text: str) -> list[tuple[str, float]]:
    """Every indexed skill, ranked by how closely its own trigger
    description matches `text` -- by meaning, an embedding comparison,
    not a judgement call. Empty when retrieval is off or the call fails;
    selection.py falls back to trigger-word matching in that case, same
    as it always has when a model-based call couldn't be made."""
    found = _post("/v1/relevant-skills", {"text": text})
    if not found:
        return []
    try:
        return [(s["skill"], s["score"]) for s in found["skills"]]
    except (KeyError, TypeError) as e:
        logger.warning("retrieval: /v1/relevant-skills answer unreadable: %s", e)
        return []


def dependencies_for(capabilities: Sequence[str]) -> dict[str, list[str]]:
    """{capability: [direct dependency, ...]} for each of `capabilities`,
    read by exact tag -- no embedding call, same reasoning as
    field_docs(). One hop only: selection.py's chain() calls this once
    per frontier step and walks the result breadth-first for the full
    transitive closure, same structure as the DEPENDS_ON dict lookup it
    replaces. Empty for a capability with no entry, same as an absent
    DEPENDS_ON key meant "no dependencies" before."""
    if not capabilities:
        return {}
    found = _post("/v1/dependencies", {"capabilities": list(capabilities)})
    if not found:
        return {}
    result: dict[str, list[str]] = {c: [] for c in capabilities}
    try:
        for p in found["passages"]:
            component, dep = p["capability"], p["symbol"].split("->", 1)[1]
            result.setdefault(component, []).append(dep)
    except (KeyError, TypeError, IndexError) as e:
        logger.warning("retrieval: /v1/dependencies answer unreadable: %s", e)
        return {}
    return result


def tool_for(capabilities: Sequence[str]) -> dict[str, str]:
    """{capability: tool name} for each of `capabilities` that has its own
    single-resource tool, read by exact tag -- no embedding call. A
    capability with no entry has no single-resource tool of its own
    (only ever built through the whole-stack tool), same as it not
    appearing in TOOL_FOR_SKILL meant before."""
    if not capabilities:
        return {}
    found = _post("/v1/tools", {"capabilities": list(capabilities)})
    if not found:
        return {}
    try:
        return {p["capability"]: p["symbol"] for p in found["passages"]}
    except (KeyError, TypeError) as e:
        logger.warning("retrieval: /v1/tools answer unreadable: %s", e)
        return {}
