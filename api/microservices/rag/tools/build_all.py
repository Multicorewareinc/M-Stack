"""Chunks EVERY file the strategy calls for -- not a sample, the whole
corpus -- and writes one chunk per line as JSON, so the result can be
counted, grepped, and inspected in full. This is still not the embedding
step: nothing here calls a model. It exists to answer, honestly, "did we
actually chunk everything," with a file someone can open and count.

    python tools/build_all.py [out_dir]     # default: a new temporary directory

Writes:
    <out_dir>/multistack.jsonl          -- every multistack/*/spec.py,
                                            driver, shared-plumbing file
                                            and README
    <out_dir>/microservices.jsonl       -- every api/microservices/*/src file
                                            and service README
    <out_dir>/skills.jsonl              -- every skill note
    <out_dir>/manifest.txt              -- one line per source file, with
                                            its chunk count, so a 0 is easy
                                            to spot without reading the JSON
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from corpus import chunk_file as _chunk_file
from corpus import corpus_files

REPO_ROOT = Path(__file__).resolve().parents[4]


def chunk_file(path: Path):
    return _chunk_file(path, repo_root=REPO_ROOT)


def run(files: list[Path], jsonl_path: Path, manifest: list[str]) -> int:
    total = 0
    with open(jsonl_path, "w") as f:
        for path in sorted(files):
            try:
                chunks = chunk_file(path)
            except SyntaxError as e:
                manifest.append(f"{0:4} chunks  SYNTAX ERROR  {path.relative_to(REPO_ROOT)}  ({e})")
                continue
            rel = path.relative_to(REPO_ROOT)
            manifest.append(f"{len(chunks):4} chunks  {rel}")
            total += len(chunks)
            for c in chunks:
                f.write(
                    json.dumps(
                        {
                            "id": c.id,
                            "kind": c.kind,
                            "capability": c.capability,
                            "source_path": c.source_path,
                            "start_line": c.start_line,
                            "end_line": c.end_line,
                            "context": c.context,
                            "text": c.text,
                        }
                    )
                    + "\n"
                )
    return total


def main() -> None:
    out_dir = (
        Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="rag-chunks-"))
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[str] = []

    selected = corpus_files(REPO_ROOT)
    multistack_files = [f for f in selected if f.relative_to(REPO_ROOT).parts[0] == "multistack"]
    micro_files = [f for f in selected if f.relative_to(REPO_ROOT).parts[0] == "api"]
    skill_files = [f for f in selected if f.name == "SKILL.md"]

    manifest.append("=== multistack/ ===")
    n1 = run(multistack_files, out_dir / "multistack.jsonl", manifest)
    manifest.append(f"-> {len(multistack_files)} files, {n1} chunks\n")

    manifest.append("=== api/microservices/*/src ===")
    n2 = run(micro_files, out_dir / "microservices.jsonl", manifest)
    manifest.append(f"-> {len(micro_files)} files, {n2} chunks\n")

    manifest.append("=== skills ===")
    n3 = run(skill_files, out_dir / "skills.jsonl", manifest)
    manifest.append(f"-> {len(skill_files)} files, {n3} chunks\n")

    total_files = len(multistack_files) + len(micro_files) + len(skill_files)
    total_chunks = n1 + n2 + n3
    manifest.append(f"TOTAL: {total_files} files -> {total_chunks} chunks")

    (out_dir / "manifest.txt").write_text("\n".join(manifest))
    print("\n".join(manifest[-6:]))
    print(f"\nWritten to {out_dir}/")


if __name__ == "__main__":
    main()
