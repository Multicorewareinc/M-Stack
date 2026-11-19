"""Builds the index: chunk the source, embed what is new, write to the vector database.

    python indexer.py --repo-root /path/to/checkout [--full]    the Multistack repository
    python indexer.py --docs /path/to/folder [--docs ...]      any folder of documents

--docs indexes every file a chunker reads under each folder -- code,
structured data and documents; chunking/__init__.py lists the types --
labelled by the folder's name, in place of the repository. What it can't
read (images, audio, legacy Office files) is reported, with the reason.

Re-running is cheap. A chunk's vector is reused when its text, under the
same model, is unchanged, so an edit to one file re-embeds only that file's
chunks. Each build is written to a new collection, and searches move to it
in one step only once it is complete; a build that fails part-way leaves
searches on the previous one. Earlier builds are then dropped.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from chunking import Chunk, Limits, chunk_document, document_files, skipped_files
from corpus import (
    chunk_file,
    corpus_files,
    dependency_chunks,
    skill_counterexample_chunks,
    skill_intent_chunks,
    tool_mapping_chunks,
)
from embeddings import DEFAULT_DOCUMENT_TEMPLATE, Embedder, EmbeddingConfig, EmbeddingError
from records import Record, git_commit, point_id, source_is_modified
from sparse import average_length, document_vector, term_id, tokenize
from store import QdrantStore, StoreError

from settings import Settings


@dataclass(frozen=True)
class BuildStats:
    total: int
    reused: int
    embedded: int
    removed: int


def build_index(
    chunks: Sequence[Chunk],
    store: QdrantStore,
    embedder,
    *,
    model: str,
    dimension: int,
    commit: str | None = None,
    commit_modified: bool | None = None,
    reuse: bool = True,
    on_batch: Callable[[int, int], None] | None = None,
    document_template: str = DEFAULT_DOCUMENT_TEMPLATE,
) -> BuildStats:
    # A vector depends on how the chunk was wrapped for the model, so a new
    # wrapping is a new vector; the plain one keeps the key it always had.
    key = (
        model if document_template == DEFAULT_DOCUMENT_TEMPLATE else f"{model}\0{document_template}"
    )
    records = [Record.from_chunk(c, key) for c in chunks]
    ids = [point_id(r.id) for r in records]
    if len(set(ids)) != len(ids):
        raise ValueError("two chunks share an id; ids must be unique")

    store.ensure_meta()
    previous = store.current()
    stored = store.existing(previous) if reuse and previous else {}
    reusable = {content: vector for content, vector in stored.values()}

    to_embed: dict[str, Chunk] = {}
    for record, chunk in zip(records, chunks, strict=True):
        if record.content_hash not in reusable:
            to_embed.setdefault(record.content_hash, chunk)
    fresh = dict(
        zip(
            to_embed,
            embedder.embed_documents(list(to_embed.values()), on_batch=on_batch),
            strict=True,
        )
    )
    vector_for = {**reusable, **fresh}

    token_lists = [tokenize(r.embed_text) for r in records]
    average = average_length(token_lists)
    built_at = datetime.now(UTC)
    target = f"{store.collection}_{built_at:%Y%m%d%H%M%S}_{uuid.uuid4().hex[:6]}"
    store.create_collection(target, dimension)
    try:
        store.upsert(
            target,
            [
                {
                    "id": pid,
                    "vector": {
                        "dense": vector_for[r.content_hash],
                        "keywords": document_vector(tokens, average),
                    },
                    "payload": r.payload(),
                }
                for pid, r, tokens in zip(ids, records, token_lists, strict=True)
            ],
        )
        store.point_to(target)
    except Exception:
        # Searches still have the previous build; this one is dropped. The
        # exception: the first switch from a pre-alias index, which drops
        # that index first -- then this build is all there is, and is kept
        # for the next run to reuse.
        with contextlib.suppress(StoreError):
            if store.current() is not None:
                store.drop(target)
        raise

    store.write_meta(
        {
            "model": model,
            "dimension": dimension,
            "chunks": len(records),
            "commit": commit,
            # True: built from uncommitted changes on top of `commit`.
            "commit_modified": commit_modified,
            "collection": target,
            "built_at": built_at.isoformat(timespec="seconds"),
            "terms": sorted({term_id(t) for tokens in token_lists for t in tokens}),
        }
    )
    for old in store.builds():
        if old != target:
            store.drop(old)

    current_ids = set(ids)
    removed = sum(1 for pid in stored if pid not in current_ids)
    reused = sum(1 for r in records if r.content_hash in reusable)
    return BuildStats(total=len(records), reused=reused, embedded=len(to_embed), removed=removed)


def collect(args: argparse.Namespace, limits: Limits) -> tuple[list[Chunk], Path | None]:
    """The chunks to index, and the repository they came from, if any."""
    if args.docs:
        chunks: list[Chunk] = []
        for folder in (d.resolve() for d in args.docs):
            for reason, paths in skipped_files(folder).items():
                print(f"  not read, {reason}: {len(paths)} file(s)", file=sys.stderr)
            for path in document_files(folder):
                rel = f"{folder.name}/{path.relative_to(folder).as_posix()}"
                try:
                    chunks.extend(chunk_document(path, rel, capability=folder.name, limits=limits))
                except (ValueError, SyntaxError, OSError) as e:
                    print(f"  skipped {rel}: {e}", file=sys.stderr)
        return chunks, None
    repo_root = args.repo_root.resolve()
    chunks = [
        c
        for path in corpus_files(repo_root)
        for c in chunk_file(path, repo_root=repo_root, limits=limits)
    ]
    chunks.extend(skill_intent_chunks(repo_root))
    chunks.extend(dependency_chunks(repo_root))
    chunks.extend(tool_mapping_chunks(repo_root))
    chunks.extend(skill_counterexample_chunks(repo_root))
    return chunks, repo_root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the reference index.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--repo-root", type=Path, help="checkout of the Multistack repository")
    source.add_argument(
        "--docs", type=Path, action="append", help="a folder of documents; repeat for more"
    )
    parser.add_argument(
        "--full", action="store_true", help="re-embed everything, ignoring what is stored"
    )
    args = parser.parse_args(argv)

    settings = Settings()
    limits = Limits(settings.chunk_max_tokens, settings.chunk_overlap_tokens)
    chunks, repo_root = collect(args, limits)
    print(f"{len(chunks)} chunks from {repo_root or ', '.join(str(d) for d in args.docs)}")

    config = EmbeddingConfig(
        url=settings.embedding_url,
        model=settings.embedding_model,
        batch_size=settings.embedding_batch_size,
        timeout_seconds=settings.embedding_timeout_seconds,
        query_instruction=settings.query_instruction,
        expected_dimension=settings.embedding_dimension,
        query_template=settings.query_template,
        document_template=settings.document_template,
        max_input_tokens=settings.embedding_max_input_tokens,
    )
    if not config.url or not config.model:
        print("EMBEDDING_URL and EMBEDDING_MODEL must be set", file=sys.stderr)
        return 1
    store = QdrantStore(
        settings.qdrant_url,
        settings.qdrant_collection,
        api_key=settings.qdrant_api_key,
        timeout=max(settings.qdrant_timeout_seconds, 60.0),
    )
    try:
        stats = build_index(
            chunks,
            store,
            Embedder(config),
            model=config.model,
            dimension=config.expected_dimension,
            commit=git_commit(repo_root) if repo_root else None,
            commit_modified=source_is_modified(repo_root) if repo_root else None,
            reuse=not args.full,
            on_batch=lambda done, total: print(f"  embedded {done}/{total}", end="\r", flush=True),
            document_template=config.document_template,
        )
    except (EmbeddingError, StoreError) as e:
        print(f"\nfailed: {e}", file=sys.stderr)
        return 1
    print(
        f"\n{stats.total} chunks: {stats.reused} reused, {stats.embedded} embedded, {stats.removed} removed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
