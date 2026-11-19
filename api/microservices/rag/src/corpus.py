"""The Multistack corpus: which files of the repository are indexed, and
the component ("capability") each chunk belongs to.

The cutting itself is chunking.py, which knows nothing of this repository.
What is added here: the capability label, read from where a file sits;
the skill notes' own shape (an overview, then one chunk per rule); and
what is left out -- this service, retired SDK code, disabled skill notes,
and everything outside the SDK, the services and the skill notes.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

from chunking import (
    DEFAULT_LIMITS,
    Chunk,
    Limits,
    chunk_document,
    chunk_markdown,
    markdown_parts,
    redact,
)

__all__ = [
    "SKILL_KINDS",
    "Chunk",
    "chunk_file",
    "chunk_skill_file",
    "corpus_files",
    "dependency_chunks",
    "origin",
    "skill_counterexample_chunks",
    "skill_intent_chunks",
    "tool_mapping_chunks",
]


def origin(source_path: str) -> str:
    """Which part of the platform a file is: "sdk" (what a plan is built
    from), "service" (what runs once it's deployed, and so decides which
    values actually work), "skill" (the agent's notes), or "other"."""
    parts = Path(source_path).parts
    if "skills" in parts and parts[-1] == "SKILL.md":
        return "skill"
    if "multistack" in parts:
        return "sdk"
    if "microservices" in parts:
        return "service"
    return "other"


# The same component is named differently in the SDK, the services and the
# skill notes. Every chunk is labelled with the skill name, so a search can
# be narrowed or boosted by capability without translating.
_SKILL_NAME = {
    "database": "cnpg",
    "cache": "valkey",
    "ingress_gateway": "ingress-gateway",
    "model-gateway": "gateway",
    "rate-limiter-rpm": "policy",
    "rate-limiter-tpm": "policy",
    "admin-control-plane": "controlplane",
    "organization-control-plane": "controlplane",
    "nats": "queue",
}

# Single files that belong to a capability without living in its directory.
_SDK_FILE_SKILL = {
    ("backends", "rke2_client"): "rke2-cluster",
    ("backends", "seaweedfs_client"): "seaweedfs",
    ("core", "rke2"): "rke2-cluster",
    ("core", "seaweedfs"): "seaweedfs",
}

# Shared plumbing: belongs to no single capability.
_SHARED_DIRS = {"backends", "core", "helm", "state", "netpolicy", "servicemonitor"}


def _capability_from_path(path: Path) -> str | None:
    """multistack/database/spec.py -> "cnpg". None for shared plumbing,
    including files that sit directly in multistack/ (stack.py, kube.py):
    those have no directory to take a name from."""
    parts = path.parts
    if "multistack" in parts:
        rest = parts[parts.index("multistack") + 1 :]
        if len(rest) < 2:
            return None
        name = rest[0]
        if (name, Path(rest[-1]).stem) in _SDK_FILE_SKILL:
            return _SDK_FILE_SKILL[(name, Path(rest[-1]).stem)]
        if name in _SHARED_DIRS:
            return None
        return _SKILL_NAME.get(name, name)
    if "microservices" in parts:
        rest = parts[parts.index("microservices") + 1 :]
        return _SKILL_NAME.get(rest[0], rest[0]) if rest else None
    return None


# --------------------------------------------------------------------------
# Skill notes (agentic/backend/src/skills/*/SKILL.md). The original plan
# was "one chunk per '## ' section" -- wrong, found by actually running
# it and looking: 16 of 18 files have ZERO "## " headings. Every file
# checked instead follows one real shape: a title, prose, one fenced
# code block showing the class shape, then "Rules when generating
# code:" followed by a flat bulleted list, where each top-level bullet
# is already one complete, self-contained rule. That bullet is the true
# atomic unit here, not a heading that doesn't exist.
# --------------------------------------------------------------------------

# Two conventions found in real files, not assumed: 17 of 18 skill files
# use a bare "Rules when generating code:" line; queue/SKILL.md (written
# separately, before this convention was settled) uses a "## Rules"
# heading instead. Both mean the same thing, so both are recognised
# rather than fixed by editing content to match the chunker.
_RULES_MARKER = re.compile(r"^(?:Rules when generating code:|## Rules)\s*$", re.MULTILINE)
_TOP_BULLET = re.compile(r"^- ", re.MULTILINE)  # top-level only; nested bullets are "  - "


def chunk_skill_file(
    path: Path, *, repo_root: Path, limits: Limits = DEFAULT_LIMITS
) -> Iterator[Chunk]:
    rel = path.relative_to(repo_root).as_posix()
    capability = path.parent.name  # skills/<name>/SKILL.md
    text = path.read_text()

    marker = _RULES_MARKER.search(text)
    if not marker:
        # Doesn't match the shape every file was found to have: chunked as
        # any Markdown is, by its headings, rather than by a guessed shape.
        for chunk in chunk_markdown(text, rel, capability=capability, limits=limits):
            yield replace(chunk, kind="skill")
        return

    # Everything before the rules list -- the title, the prose, and the
    # fenced code block showing the real class shape -- split only if it
    # is too long for one chunk.
    overview_lines = text[: marker.start()].rstrip().splitlines()
    yield from markdown_parts(
        Chunk(
            id=f"{rel}:1:overview",
            text="",
            context=f"overview and class shape for the {capability} skill, in {rel}",
            capability=capability,
            source_path=rel,
            start_line=1,
            end_line=len(overview_lines),
            kind="skill",
        ),
        overview_lines,
        1,
        limits,
    )

    # One chunk per top-level rule bullet. A bullet runs until the next
    # top-level bullet (nested "  - " lines and wrapped continuation
    # lines stay attached to the bullet they belong to).
    rules_text = text[marker.end() :]
    bullets = list(_TOP_BULLET.finditer(rules_text))
    base_line = text.count("\n", 0, marker.end()) + 1
    for i, m in enumerate(bullets):
        end = bullets[i + 1].start() if i + 1 < len(bullets) else len(rules_text)
        bullet_text = rules_text[m.start() : end].rstrip()
        start_line = base_line + rules_text.count("\n", 0, m.start())
        # First few words as a label, so the context line says roughly
        # what the rule is about, not just "a rule".
        label = re.sub(r"[*`]", "", bullet_text[2:80].split("\n")[0]).strip()
        yield from markdown_parts(
            Chunk(
                id=f"{rel}:{start_line}:rule",
                text="",
                context=f"a rule for the {capability} skill ({label}...), in {rel}",
                capability=capability,
                source_path=rel,
                start_line=start_line,
                end_line=start_line + bullet_text.count("\n"),
                kind="rule",
            ),
            bullet_text.splitlines(),
            start_line,
            limits,
        )


# Kinds that come from the skill notes rather than from code.
SKILL_KINDS = ("skill", "rule")

SEARCH_SERVICE = "rag"

# SDK code for a capability the platform has replaced. It is still in the
# SDK, but a passage from it would steer an answer towards something that
# can no longer be deployed: MinIO, replaced by SeaweedFS once its images
# needed an authenticated pull.
RETIRED_SDK_FILES = frozenset({"multistack/core/minio.py", "multistack/backends/minio_client.py"})


def _enabled_skill(path: Path) -> bool:
    """False for a skill note switched off with `enabled: false` in its
    frontmatter: the agent never loads it, so its rules must not reach an
    answer through search either."""
    text = path.read_text()
    if not text.startswith("---") or text.count("---") < 2:
        return True
    frontmatter = text.split("---", 2)[1]
    return not re.search(r"^enabled:\s*false\s*$", frontmatter, re.MULTILINE | re.IGNORECASE)


def corpus_files(repo_root: Path) -> list[Path]:
    """Every file that gets chunked. The single definition of what is in:
    the SDK, each service's src/, their READMEs, and the skill notes. Keeping it to
    those globs is also what keeps migrations, charts, tests, UI code and
    every .env file out. This service is left out: it searches the
    platform, and is not a component anyone configures. So is the SDK
    code of retired capabilities (RETIRED_SDK_FILES)."""
    sdk = [
        f
        for f in repo_root.glob("multistack/**/*.py")
        if "__pycache__" not in f.parts
        and f.relative_to(repo_root).as_posix() not in RETIRED_SDK_FILES
    ]
    services = [
        f
        for f in repo_root.glob("api/microservices/*/src/**/*.py")
        if "__pycache__" not in f.parts and f.relative_to(repo_root).parts[2] != SEARCH_SERVICE
    ]
    readmes = [
        f
        for f in [
            *repo_root.glob("multistack/**/README.md"),
            *repo_root.glob("api/microservices/*/README.md"),
        ]
        if "api/microservices/" + SEARCH_SERVICE + "/" not in f.relative_to(repo_root).as_posix()
    ]
    skills = [
        f for f in repo_root.glob("agentic/backend/src/skills/*/SKILL.md") if _enabled_skill(f)
    ]
    return sorted([*sdk, *services, *readmes, *skills])


_DESCRIPTION = re.compile(r"^description:\s*(.+?)\s*$", re.MULTILINE)


def skill_intent_chunks(repo_root: Path) -> list[Chunk]:
    """One chunk per enabled skill: just its own `description:` line from
    SKILL.md's frontmatter -- the same sentence orchestration/selection.py
    used to search for trigger words by hand ("use this skill when the
    user's request mentions rate limiting, a rate limiter, RPM, ...").
    Embeds each description so a request can be compared against it the
    same way a question is compared against any other passage: by meaning,
    not a maintained word list.

    NOT CURRENTLY USED by selection.py's live path (service.py's
    rank_skills() / router.py's /v1/relevant-skills): an early version of
    selection did embedding recall over these before a confirmation call,
    then collapsed to one direct judgement call over all ~19 skills at
    once -- cheap enough in context to skip a retrieval pass entirely.
    Left in place, not removed, as the right tool for when the skill count
    grows large enough that listing all of them again needs narrowing
    first -- the exact condition selection.py's own docstring already
    names for when embeddings earn their keep over a direct call."""
    chunks: list[Chunk] = []
    for path in sorted(repo_root.glob("agentic/backend/src/skills/*/SKILL.md")):
        if not _enabled_skill(path):
            continue
        text = path.read_text()
        if not text.startswith("---") or text.count("---") < 2:
            continue
        match = _DESCRIPTION.search(text.split("---", 2)[1])
        if not match:
            continue
        capability = path.parent.name
        rel = path.relative_to(repo_root).as_posix()
        chunks.append(
            Chunk(
                id=f"{rel}:intent",
                text=redact(match.group(1)),
                context=f"what the {capability} skill is for",
                capability=capability,
                source_path=rel,
                start_line=1,
                end_line=1,
                kind="skill_intent",
            )
        )
    return chunks


def _module_level_dict(path: Path, name: str) -> dict | None:
    """The literal value of the module-level assignment `name = {...}` in
    the Python source at `path`, or None if it isn't there.

    Reads the other service's source as plain text and parses it with
    `ast`, the same way skill_intent_chunks() reads that service's
    SKILL.md files as plain text -- this service never imports agentic's
    code as a library (different deployable unit, no shared venv
    guarantee), only reads its files from the same checkout."""
    if not path.exists():
        return None
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            try:
                return ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                return None
    return None


def dependency_chunks(repo_root: Path) -> list[Chunk]:
    """One chunk per (component, dependency) edge in mcp_tools/architecture.py's
    DEPENDS_ON, text = that edge's own real explanation from the same
    file's REASON map. Read by field_docs()-style exact lookup
    (kind="dependency", capability=<component>), never searched: a
    multi-hop dependency chain has to come back complete every time, so
    the lookup that decides which edges exist stays a tag filter, not a
    similarity ranking that could drop a hop. See orchestration/selection.py's
    chain() for the BFS that walks these edges."""
    src = repo_root / "agentic/backend/src/mcp_tools/architecture.py"
    depends_on = _module_level_dict(src, "DEPENDS_ON") or {}
    reason = _module_level_dict(src, "REASON") or {}
    rel = src.relative_to(repo_root).as_posix()
    chunks: list[Chunk] = []
    for component, deps in depends_on.items():
        for dep in deps:
            text = reason.get((component, dep), f"{component} needs {dep}")
            chunks.append(
                Chunk(
                    id=f"{rel}:dependency:{component}:{dep}",
                    text=f"{component} needs {dep}: {text}",
                    context=f"why the {component} capability depends on {dep}",
                    capability=component,
                    source_path=rel,
                    start_line=1,
                    end_line=1,
                    kind="dependency",
                    symbol=f"{component}->{dep}",
                )
            )
    return chunks


def tool_mapping_chunks(repo_root: Path) -> list[Chunk]:
    """One chunk per entry in orchestration/selection.py's TOOL_FOR_SKILL:
    which MCP tool builds a given capability on its own. Read by exact
    lookup (kind="tool_mapping", capability=<skill>), same reasoning as
    dependency_chunks() -- this is a fixed naming convention, not
    something with any ambiguity to resolve by meaning."""
    src = repo_root / "agentic/backend/src/orchestration/selection.py"
    tool_for_skill = _module_level_dict(src, "TOOL_FOR_SKILL") or {}
    rel = src.relative_to(repo_root).as_posix()
    chunks: list[Chunk] = []
    for skill, tool_name in tool_for_skill.items():
        chunks.append(
            Chunk(
                id=f"{rel}:tool_mapping:{skill}",
                text=f"{skill} is built by the {tool_name} tool.",
                context=f"which MCP tool builds the {skill} capability on its own",
                capability=skill,
                source_path=rel,
                start_line=1,
                end_line=1,
                kind="tool_mapping",
                symbol=tool_name,
            )
        )
    return chunks


def skill_counterexample_chunks(repo_root: Path) -> list[Chunk]:
    """One chunk per entry in orchestration/selection.py's FALSE_MATCHES: a
    real sentence that scores deceptively high against a skill by topic
    alone, without actually requesting it. Unlike dependency_chunks()/
    tool_mapping_chunks(), this kind ("skill_counterexample") IS meant to
    be searched by embedding, not read by exact tag -- see service.py's
    rank_skills(), which subtracts a request's best-matching
    counterexample score from its positive skill_intent score, so a
    sentence merely ABOUT a skill's topic doesn't outscore one actually
    asking FOR it (the module's own measured case: a gateway fail-open
    sentence scored 0.734 against policy, higher than a genuine
    rate-limiting request's 0.694)."""
    src = repo_root / "agentic/backend/src/orchestration/selection.py"
    false_matches = _module_level_dict(src, "FALSE_MATCHES") or {}
    rel = src.relative_to(repo_root).as_posix()
    chunks: list[Chunk] = []
    for skill, examples in false_matches.items():
        for i, example in enumerate(examples):
            chunks.append(
                Chunk(
                    id=f"{rel}:skill_counterexample:{skill}:{i}",
                    text=example,
                    context=f"a sentence that looks like a {skill} request but isn't",
                    capability=skill,
                    source_path=rel,
                    start_line=1,
                    end_line=1,
                    kind="skill_counterexample",
                )
            )
    return chunks


def chunk_file(path: Path, *, repo_root: Path, limits: Limits = DEFAULT_LIMITS) -> list[Chunk]:
    rel = path.relative_to(repo_root)
    if path.name == "SKILL.md":
        return [
            replace(c, text=redact(c.text))
            for c in chunk_skill_file(path, repo_root=repo_root, limits=limits)
        ]
    return chunk_document(
        path, rel.as_posix(), capability=_capability_from_path(path), limits=limits
    )
