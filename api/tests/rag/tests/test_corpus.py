"""The chunker: small files with known contents, then the real repo.

Every case here is a bug that was found by reading real chunks: a field's
comment dropped, skill notes cut on headings they don't have, a config
class losing its fields, and module-level constants missing from spec
files."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from corpus import chunk_file, chunk_skill_file, corpus_files

REPO = Path(__file__).resolve().parents[4]
# Only a checkout has the source these tests read; the Docker test stage mounts tests/ alone.
IN_A_CHECKOUT = (REPO / "multistack").is_dir() and (REPO / "agentic").is_dir()

SPEC = '''"""Spec module for a thing."""
from typing import Optional

# Pinned by digest, not tag.
DEFAULT_IMAGE = (
    "registry/img"
    "@sha256:abc123"
)


class Options:
    """Deployment settings."""

    name: str = "x"
    # Full image path. None keeps the chart's default registry.
    image_repository: Optional[str] = None
'''

SERVICE = '''"""Service configuration."""

LIMIT = 5


class Settings(BaseSettings):
    log_level: str = "INFO"
    # Where the cache lives.
    valkey_url: str = ""


def helper(x):
    """Adds one."""
    return x + 1


class Thing:
    def method(self):
        return 1
'''

SKILL = """---
name: demo
description: Use this skill when the request mentions demo.
---

# Demo skill

Intro prose.

```python
class Demo:
    a: int = 1
```

Rules when generating code:
- First rule, one line.
- Second rule that wraps
  onto a continuation line.
- Third rule.
"""


def write(tmp_path: Path, rel: str, text: str) -> Path:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def by_context(chunks, needle: str):
    matches = [c for c in chunks if needle in c.context]
    assert len(matches) == 1, (
        f"expected one chunk for {needle!r}, got {[c.context for c in chunks]}"
    )
    return matches[0]


# ---- spec files


def test_a_field_keeps_the_comment_above_it(tmp_path):
    chunks = list(chunk_file(write(tmp_path, "multistack/demo/spec.py", SPEC), repo_root=tmp_path))
    field = by_context(chunks, "field 'image_repository'")
    assert "Full image path" in field.text
    assert field.text.endswith("image_repository: Optional[str] = None")


def test_spec_files_chunk_module_constants_with_their_comment(tmp_path):
    """The default image pin and the reason for it live at module level,
    where the class walk never looks."""
    chunks = list(chunk_file(write(tmp_path, "multistack/demo/spec.py", SPEC), repo_root=tmp_path))
    constant = by_context(chunks, "constant DEFAULT_IMAGE")
    assert "Pinned by digest" in constant.text
    assert "@sha256:abc123" in constant.text


def test_spec_files_chunk_the_module_docstring(tmp_path):
    chunks = list(chunk_file(write(tmp_path, "multistack/demo/spec.py", SPEC), repo_root=tmp_path))
    assert by_context(chunks, "module overview").text == "Spec module for a thing."


def test_a_spec_class_gets_an_overview_chunk(tmp_path):
    chunks = list(chunk_file(write(tmp_path, "multistack/demo/spec.py", SPEC), repo_root=tmp_path))
    (overview,) = [c for c in chunks if c.kind == "class"]
    assert overview.context == "class Options, in multistack/demo/spec.py"
    assert "Deployment settings." in overview.text


# ---- other python


def test_a_config_class_with_only_fields_keeps_every_field(tmp_path):
    chunks = list(
        chunk_file(
            write(tmp_path, "api/microservices/x/src/settings.py", SERVICE), repo_root=tmp_path
        )
    )
    assert "log_level: str" in by_context(chunks, "field 'log_level'").text
    assert "Where the cache lives" in by_context(chunks, "field 'valkey_url'").text


def test_functions_and_methods_are_separate_chunks(tmp_path):
    chunks = list(
        chunk_file(
            write(tmp_path, "api/microservices/x/src/settings.py", SERVICE), repo_root=tmp_path
        )
    )
    assert by_context(chunks, "function 'helper'").text.startswith("def helper")
    assert "return 1" in by_context(chunks, "method 'method' of class Thing").text


def test_python_files_chunk_module_constants(tmp_path):
    chunks = list(
        chunk_file(
            write(tmp_path, "api/microservices/x/src/settings.py", SERVICE), repo_root=tmp_path
        )
    )
    assert by_context(chunks, "constant LIMIT").text == "LIMIT = 5"


def test_a_file_with_only_imports_produces_nothing(tmp_path):
    path = write(tmp_path, "multistack/demo/__init__.py", "from .spec import Options\n")
    assert list(chunk_file(path, repo_root=tmp_path)) == []


# ---- skill notes


def test_skill_notes_split_into_an_overview_and_one_chunk_per_rule(tmp_path):
    chunks = list(
        chunk_skill_file(
            write(tmp_path, "agentic/backend/src/skills/demo/SKILL.md", SKILL), repo_root=tmp_path
        )
    )
    assert len(chunks) == 4
    assert [c.kind for c in chunks] == ["skill", "rule", "rule", "rule"]
    overview = chunks[0]
    assert "# Demo skill" in overview.text and "class Demo:" in overview.text
    assert "Rules when generating code" not in overview.text
    assert [c.text.splitlines()[0] for c in chunks[1:]] == [
        "- First rule, one line.",
        "- Second rule that wraps",
        "- Third rule.",
    ]


def test_a_wrapped_rule_stays_in_one_chunk(tmp_path):
    chunks = list(
        chunk_skill_file(
            write(tmp_path, "agentic/backend/src/skills/demo/SKILL.md", SKILL), repo_root=tmp_path
        )
    )
    assert "onto a continuation line." in chunks[2].text


def test_the_rules_heading_form_is_recognised_too(tmp_path):
    text = SKILL.replace("Rules when generating code:", "## Rules")
    chunks = list(
        chunk_skill_file(
            write(tmp_path, "agentic/backend/src/skills/demo/SKILL.md", text), repo_root=tmp_path
        )
    )
    assert len(chunks) == 4


def test_notes_without_a_rules_section_are_chunked_by_their_headings(tmp_path):
    chunks = list(
        chunk_skill_file(
            write(tmp_path, "agentic/backend/src/skills/demo/SKILL.md", "# Just prose\n\nNotes.\n"),
            repo_root=tmp_path,
        )
    )
    assert [c.kind for c in chunks] == ["skill"]


def test_every_chunk_records_where_it_came_from(tmp_path):
    path = write(tmp_path, "multistack/demo/spec.py", SPEC)
    for chunk in chunk_file(path, repo_root=tmp_path):
        assert chunk.source_path == "multistack/demo/spec.py"
        assert 1 <= chunk.start_line <= chunk.end_line
        assert chunk.embed_text == f"{chunk.context}\n\n{chunk.text}"


# ---- capability labels


@pytest.mark.parametrize(
    "relative, expected",
    [
        ("multistack/database/spec.py", "cnpg"),
        ("multistack/cache/drivers/valkey.py", "valkey"),
        ("multistack/ingress_gateway/spec.py", "ingress-gateway"),
        ("multistack/gateway/spec.py", "gateway"),
        ("multistack/queue/drivers/nats/deployment/manager.py", "queue"),
        ("multistack/backends/seaweedfs_client.py", "seaweedfs"),
        ("multistack/core/seaweedfs.py", "seaweedfs"),
        ("multistack/backends/rke2_client.py", "rke2-cluster"),
        ("multistack/core/rke2.py", "rke2-cluster"),
        ("api/microservices/model-gateway/src/main.py", "gateway"),
        ("api/microservices/rate-limiter-tpm/src/main.py", "policy"),
        ("api/microservices/organization-control-plane/src/main.py", "controlplane"),
        ("api/microservices/admin-control-plane/src/main.py", "controlplane"),
        ("api/microservices/billing/src/main.py", "billing"),
    ],
)
def test_every_component_is_labelled_with_its_skill_name(tmp_path, relative, expected):
    path = write(tmp_path, relative, "X = 1\n")
    assert [c.capability for c in chunk_file(path, repo_root=tmp_path)] == [expected]


@pytest.mark.parametrize(
    "relative",
    [
        "multistack/stack.py",
        "multistack/kube.py",
        "multistack/__init__.py",
        "multistack/helm/manager.py",
        "multistack/state/store.py",
        "multistack/core/other.py",
        "multistack/backends/transport.py",
    ],
)
def test_shared_plumbing_has_no_capability(tmp_path, relative):
    """A file directly in multistack/ has no directory to take a name from;
    its own filename used to be taken instead."""
    path = write(tmp_path, relative, "X = 1\n")
    assert [c.capability for c in chunk_file(path, repo_root=tmp_path)] == [None]


# ---- the real repo


@pytest.fixture(scope="module")
def corpus():
    if not IN_A_CHECKOUT:
        pytest.skip("reads the repository's own source; run from a checkout")
    files = corpus_files(REPO)
    return files, {f: chunk_file(f, repo_root=REPO) for f in files}


def test_what_is_selected(corpus):
    files, _ = corpus
    relative = [f.relative_to(REPO).as_posix() for f in files]
    assert any(r.startswith("multistack/") for r in relative)
    assert any(r.startswith("api/microservices/") and "/src/" in r for r in relative)
    skill_notes = sum(
        "enabled: false" not in f.read_text()
        for f in (REPO / "agentic/backend/src/skills").glob("*/SKILL.md")
    )
    assert sum(r.endswith("SKILL.md") for r in relative) == skill_notes


def test_nothing_excluded_by_design_is_selected(corpus):
    files, _ = corpus
    for f in files:
        parts = f.relative_to(REPO).parts
        assert f.name != ".env" and not f.name.startswith(".env"), f
        assert "migrations" not in parts and "chart" not in parts and "tests" not in parts, f
        assert parts[0] in {"multistack", "api", "agentic"}, f
        assert f.suffix == ".py" or f.name in {"README.md", "SKILL.md"}, f


def test_chunk_ids_are_unique_across_the_whole_corpus(corpus):
    """The id is what a stored vector will be keyed on."""
    _, by_file = corpus
    ids = [c.id for chunks in by_file.values() for c in chunks]
    duplicates = {i for i in ids if ids.count(i) > 1}
    assert not duplicates, sorted(duplicates)[:5]


def test_no_chunk_is_empty(corpus):
    _, by_file = corpus
    empty = [c.id for chunks in by_file.values() for c in chunks if not c.text.strip()]
    assert not empty, empty[:5]


def test_only_pure_reexport_files_produce_nothing(corpus):
    _, by_file = corpus
    for path, chunks in by_file.items():
        if chunks or path.stat().st_size <= 50:
            continue
        code = [
            line
            for line in path.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith(("#", '"""'))
            # A bare `if __name__ == "__main__":` guard around a call to a
            # function already imported above is the standard entry-point
            # idiom -- as contentless as a pure reexport, just a different
            # shape of it (confirmed real: multistack/state/viewer/__main__.py
            # is exactly `from .server import main` then this guard).
            and line.strip() != 'if __name__ == "__main__":'
        ]
        assert all(
            line.startswith(("from ", "import ", "__all__", " ", "]", ")", '"')) for line in code
        ), f"{path} has real content but produced no chunks"


def test_no_chunk_is_too_long_for_the_embedding_server(corpus):
    """The server accepts 4096 tokens; three characters per token is a
    pessimistic estimate for code."""
    _, by_file = corpus
    too_long = [
        (c.id, len(c.embed_text))
        for chunks in by_file.values()
        for c in chunks
        if len(c.embed_text) > 4096 * 3
    ]
    assert not too_long, too_long


SECRET_SHAPES = [
    re.compile(r"gsk_[A-Za-z0-9]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]


def test_no_chunk_contains_anything_shaped_like_a_credential(corpus):
    _, by_file = corpus
    found = [
        (c.id, pattern.pattern)
        for chunks in by_file.values()
        for c in chunks
        for pattern in SECRET_SHAPES
        if pattern.search(c.text)
    ]
    assert not found, found


def test_the_pinned_llamacpp_image_and_its_reason_are_findable(corpus):
    _, by_file = corpus
    chunks = by_file[REPO / "multistack/inference/spec.py"]
    constant = by_context(chunks, "constant DEFAULT_LLAMACPP_IMAGE")
    assert "pinned by digest" in constant.text and "@sha256:" in constant.text


def test_a_helm_repository_list_is_not_lost(corpus):
    _, by_file = corpus
    chunks = by_file[REPO / "multistack/helm/repositories.py"]
    assert "34.8s" in by_context(chunks, "module overview").text
    assert by_context(chunks, "constant DEFAULT_REPOSITORIES")


def test_every_label_in_the_real_corpus_is_a_skill_name_or_none(corpus):
    _, by_file = corpus
    skills = {f.parent.name for f in (REPO / "agentic/backend/src/skills").glob("*/SKILL.md")}
    labels = {c.capability for chunks in by_file.values() for c in chunks}
    assert labels - {None} <= skills, sorted(labels - {None} - skills)


def test_the_search_service_is_not_part_of_the_corpus(tmp_path):
    write(tmp_path, "api/microservices/rag/src/service.py", "def f():\n    return 1\n")
    write(tmp_path, "api/microservices/billing/src/service.py", "def g():\n    return 1\n")
    names = [f.relative_to(tmp_path).as_posix() for f in corpus_files(tmp_path)]
    assert names == ["api/microservices/billing/src/service.py"]


def test_indexed_paths_cover_every_selected_file():
    """records.INDEXED_PATHS is what a stale-index check compares; it must
    contain everything corpus_files() reads, or an edit goes unnoticed."""
    from records import INDEXED_PATHS

    if not IN_A_CHECKOUT:
        pytest.skip("reads the repository's own source; run from a checkout")
    outside = [
        f.relative_to(REPO).as_posix()
        for f in corpus_files(REPO)
        if not f.relative_to(REPO).as_posix().startswith(tuple(p + "/" for p in INDEXED_PATHS))
    ]
    assert not outside


def test_retired_sdk_code_is_not_indexed(tmp_path):
    """MinIO's SDK code is still there, but the platform replaced it; a
    passage from it would point an answer at something undeployable."""
    write(tmp_path, "multistack/core/minio.py", "class MinIOTenant:\n    x = 1\n")
    write(tmp_path, "multistack/core/seaweedfs.py", "class SeaweedFSCluster:\n    x = 1\n")
    names = [f.relative_to(tmp_path).as_posix() for f in corpus_files(tmp_path)]
    assert names == ["multistack/core/seaweedfs.py"]


def test_a_disabled_skill_note_is_not_indexed(tmp_path):
    write(tmp_path, "agentic/backend/src/skills/on/SKILL.md", "---\nname: on\n---\n# On\n")
    write(
        tmp_path,
        "agentic/backend/src/skills/off/SKILL.md",
        "---\nname: off\nenabled: false\n---\n# Off\n",
    )
    names = [f.relative_to(tmp_path).as_posix() for f in corpus_files(tmp_path)]
    assert names == ["agentic/backend/src/skills/on/SKILL.md"]


# ---- methods and properties in spec files


SPEC_WITH_METHODS = '''"""Spec."""


class Cache:
    name: str = "cache"

    @property
    def endpoint(self) -> str:
        """In-cluster URL for this cache."""
        return f"redis://{self.name}:6379/0"

    @field_validator("name")
    @classmethod
    def _not_empty(cls, value):
        return value

    def validate(self) -> None:
        if self.replicas < 2:
            raise ValueError("replicas is below the quorum")
'''


def test_a_spec_property_is_chunked_with_its_decorator(tmp_path):
    chunks = chunk_file(
        write(tmp_path, "multistack/cache/spec.py", SPEC_WITH_METHODS), repo_root=tmp_path
    )
    endpoint = by_context(chunks, "property 'endpoint' of class Cache")
    assert endpoint.text.startswith("    @property") and "redis://" in endpoint.text
    assert endpoint.kind == "function"


def test_a_spec_validator_keeps_the_field_it_validates(tmp_path):
    chunks = chunk_file(
        write(tmp_path, "multistack/cache/spec.py", SPEC_WITH_METHODS), repo_root=tmp_path
    )
    validator = by_context(chunks, "method '_not_empty' of class Cache")
    assert '@field_validator("name")' in validator.text


def test_a_spec_validate_method_carries_its_rules(tmp_path):
    chunks = chunk_file(
        write(tmp_path, "multistack/cache/spec.py", SPEC_WITH_METHODS), repo_root=tmp_path
    )
    rules = by_context(chunks, "method 'validate' of class Cache")
    assert "below the quorum" in rules.text


def test_spec_method_ids_stay_on_the_def_line(tmp_path):
    """A decorator widens the chunk, not its id: ids key re-indexing."""
    chunks = chunk_file(
        write(tmp_path, "multistack/cache/spec.py", SPEC_WITH_METHODS), repo_root=tmp_path
    )
    endpoint = by_context(chunks, "property 'endpoint' of class Cache")
    assert endpoint.start_line == 7 and endpoint.id.endswith(":8:method")


def test_a_service_route_is_chunked_with_its_decorator(tmp_path):
    router = """from fastapi import APIRouter

router = APIRouter()


@router.post("/v1/search")
def search(body):
    return body
"""
    chunks = chunk_file(
        write(tmp_path, "api/microservices/x/src/router.py", router), repo_root=tmp_path
    )
    route = by_context(chunks, "function 'search'")
    assert route.text.startswith('@router.post("/v1/search")')


def test_a_module_constant_is_described_by_its_modules_docstring(tmp_path):
    seed = '''"""Idempotent seed of the three default plan tiers. More detail here."""

# name -> (tpm, rpm, quota).
DEFAULT_PLANS = {"Free": (1, 2, 3)}
'''
    chunks = chunk_file(
        write(tmp_path, "api/microservices/x/src/seed.py", seed), repo_root=tmp_path
    )
    constant = by_context(chunks, "constant DEFAULT_PLANS")
    assert constant.context.endswith("(Idempotent seed of the three default plan tiers.)")


def test_a_constant_in_a_module_without_a_docstring_has_a_plain_description(tmp_path):
    chunks = chunk_file(
        write(tmp_path, "api/microservices/x/src/c.py", "LIMIT = 5\n"), repo_root=tmp_path
    )
    assert (
        by_context(chunks, "constant LIMIT").context
        == "constant LIMIT, in api/microservices/x/src/c.py"
    )
