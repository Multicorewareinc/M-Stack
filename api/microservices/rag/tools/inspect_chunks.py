"""Run the chunker on real files and PRINT every chunk, so a human can
read them before anything is sent to an embedding model. This is a
by-eye check, not an automated test -- api/tests/rag/tests/test_corpus.py has the
automated ones.

    python tools/inspect_chunks.py [pattern]

With no argument, runs on one representative file of each kind. Pass a
glob-ish substring to inspect a specific file instead, e.g.:

    python tools/inspect_chunks.py policy/spec.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from corpus import chunk_file

REPO_ROOT = Path(__file__).resolve().parents[4]

DEFAULT_FILES = [
    ("spec", REPO_ROOT / "multistack/gateway/spec.py"),
    ("driver", REPO_ROOT / "multistack/gateway/drivers/modelgateway.py"),
    ("service", REPO_ROOT / "api/microservices/model-gateway/src/main.py"),
    ("skill", REPO_ROOT / "agentic/backend/src/skills/gateway/SKILL.md"),
]


def chunks_for(path: Path):
    return chunk_file(path, repo_root=REPO_ROOT)


def show(label: str, path: Path) -> None:
    if not path.exists():
        print(f"!! {label}: {path} does not exist, skipping\n")
        return
    chunks = chunks_for(path)
    print("=" * 78)
    print(f"{label.upper()}  --  {path.relative_to(REPO_ROOT)}  ->  {len(chunks)} chunks")
    print("=" * 78)
    for c in chunks:
        print(c)
        print()
    sizes = [len(c.text) for c in chunks]
    if sizes:
        print(f"  sizes (chars): min={min(sizes)} max={max(sizes)} avg={sum(sizes) // len(sizes)}")
    print()


def main() -> None:
    if len(sys.argv) > 1:
        pattern = sys.argv[1]
        matches = [
            p
            for p in REPO_ROOT.rglob("*")
            if pattern in str(p)
            and p.is_file()
            and "__pycache__" not in str(p)
            and "node_modules" not in str(p)
        ]
        if not matches:
            print(f"no file matched {pattern!r}")
            return
        for path in matches[:5]:
            show(path.name, path)
        return

    for label, path in DEFAULT_FILES:
        show(label, path)


if __name__ == "__main__":
    main()
