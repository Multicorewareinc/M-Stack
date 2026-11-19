"""Building the index: what is embedded, what is reused, what is removed."""

from __future__ import annotations

import argparse
import subprocess

import pytest
from chunking import Limits
from corpus import Chunk
from indexer import build_index, collect
from records import point_id, source_is_modified
from sparse import term_id


def chunk(n: int, text: str, capability: str | None = "gateway") -> Chunk:
    return Chunk(
        id=f"f{n}.py:{n}:field",
        text=text,
        context=f"field {n}",
        capability=capability,
        source_path=f"f{n}.py",
        start_line=n,
        end_line=n,
        kind="field",
    )


class CountingEmbedder:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def embed_documents(self, chunks, *, on_batch=None):
        self.sent.extend(c.embed_text for c in chunks)
        return [[1.0, 0.0] for _ in chunks]


def build(store, chunks, embedder=None, **kwargs):
    return build_index(
        chunks, store, embedder or CountingEmbedder(), model="m", dimension=2, **kwargs
    )


def test_every_chunk_is_stored_with_both_vectors_and_its_source(memory_store):
    stats = build(memory_store, [chunk(1, "replicas: int = 2")])
    point = memory_store.points[point_id("f1.py:1:field")]
    assert set(point["vector"]) == {"dense", "keywords"}
    assert (
        point["payload"]["source_path"] == "f1.py"
        and point["payload"]["text"] == "replicas: int = 2"
    )
    assert (stats.total, stats.embedded, stats.reused, stats.removed) == (1, 1, 0, 0)


def test_unchanged_text_is_not_embedded_again(memory_store):
    build(memory_store, [chunk(1, "a"), chunk(2, "b")])
    embedder = CountingEmbedder()
    stats = build(memory_store, [chunk(1, "a"), chunk(2, "b changed")], embedder)
    assert embedder.sent == ["field 2\n\nb changed"]
    assert (stats.reused, stats.embedded) == (1, 1)


def test_full_rebuild_embeds_everything(memory_store):
    build(memory_store, [chunk(1, "a")])
    embedder = CountingEmbedder()
    build(memory_store, [chunk(1, "a")], embedder, reuse=False)
    assert len(embedder.sent) == 1


def test_identical_text_is_sent_once(memory_store):
    embedder = CountingEmbedder()
    same = [
        chunk(1, "same"),
        Chunk(
            id="g.py:1:field",
            text="same",
            context="field 1",
            capability="gateway",
            source_path="g.py",
            start_line=1,
            end_line=1,
            kind="field",
        ),
    ]
    build(memory_store, same, embedder)
    assert len(embedder.sent) == 1 and memory_store.count() == 2


def test_chunks_that_no_longer_exist_are_removed(memory_store):
    build(memory_store, [chunk(1, "a"), chunk(2, "b")])
    stats = build(memory_store, [chunk(1, "a")])
    assert stats.removed == 1 and memory_store.count() == 1


def test_metadata_records_the_model_and_the_words_indexed(memory_store):
    build(memory_store, [chunk(1, "replicas cache")], commit="abc")
    meta = memory_store.meta
    assert (meta["model"], meta["dimension"], meta["chunks"], meta["commit"]) == ("m", 2, 1, "abc")
    assert term_id("replica") in meta["terms"] and term_id("cache") in meta["terms"]


def test_duplicate_chunk_ids_are_refused(memory_store):
    with pytest.raises(ValueError, match="share an id"):
        build(memory_store, [chunk(1, "a"), chunk(1, "b")])


# ---- each build is its own collection


def test_a_rebuild_moves_searches_to_a_new_collection_and_drops_the_old(memory_store):
    build(memory_store, [chunk(1, "a")])
    first = memory_store.current()
    build(memory_store, [chunk(1, "a"), chunk(2, "b")])
    second = memory_store.current()
    assert first != second
    assert memory_store.collections() == [second]
    assert memory_store.meta["collection"] == second and memory_store.count() == 2


def test_a_build_that_fails_leaves_searches_on_the_previous_one(memory_store):
    build(memory_store, [chunk(1, "a")])
    before = memory_store.current()
    memory_store.fail_upsert = True
    with pytest.raises(Exception, match="upsert refused"):
        build(memory_store, [chunk(1, "a"), chunk(2, "b")])
    assert memory_store.current() == before and memory_store.count() == 1
    assert memory_store.collections() == [before]


def test_metadata_says_whether_the_source_had_uncommitted_changes(memory_store):
    build(memory_store, [chunk(1, "a")], commit="abc", commit_modified=True)
    assert memory_store.meta["commit_modified"] is True


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_source_is_modified_follows_the_indexed_directories_only(tmp_path):
    (tmp_path / "multistack").mkdir()
    (tmp_path / "multistack" / "spec.py").write_text("a = 1\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "x.md").write_text("x\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c")
    assert source_is_modified(tmp_path) is False
    (tmp_path / "docs" / "x.md").write_text("edited\n")
    assert source_is_modified(tmp_path) is False
    (tmp_path / "multistack" / "new.py").write_text("b = 2\n")
    assert source_is_modified(tmp_path) is True


def test_source_is_modified_is_unknown_outside_git(tmp_path):
    assert source_is_modified(tmp_path) is None


def test_a_new_document_wrapping_embeds_everything_again(memory_store):
    build(memory_store, [chunk(1, "a")])
    embedder = CountingEmbedder()
    stats = build(memory_store, [chunk(1, "a")], embedder, document_template="passage: {text}")
    assert (stats.reused, stats.embedded) == (0, 1)


def test_a_folder_of_documents_is_indexed_labelled_by_the_folders_name(tmp_path):
    docs = tmp_path / "handbook"
    (docs / "ops").mkdir(parents=True)
    (docs / "ops" / "runbook.md").write_text("# Restart\n\nScale to zero, then back.\n")
    (docs / "notes.txt").write_text("On-call rotates weekly.\n")
    (docs / ".env").write_text("TOKEN=abc\n")
    (docs / "diagram.png").write_bytes(b"\x89PNG")
    args = argparse.Namespace(docs=[docs], repo_root=None)
    chunks, repo_root = collect(args, Limits())
    assert repo_root is None
    assert sorted(c.source_path for c in chunks) == [
        "handbook/notes.txt",
        "handbook/ops/runbook.md",
    ]
    assert {c.capability for c in chunks} == {"handbook"}


def test_a_folder_run_reports_what_it_could_not_read(tmp_path, capsys):
    docs = tmp_path / "handbook"
    docs.mkdir()
    (docs / "diagram.png").write_bytes(b"\x89PNG")
    (docs / "notes.md").write_text("# Notes\n\nText.\n")
    collect(argparse.Namespace(docs=[docs], repo_root=None), Limits())
    assert "not read, an image; reading its text needs OCR: 1 file(s)" in capsys.readouterr().err


def test_a_document_that_cannot_be_read_is_skipped_not_fatal(tmp_path, capsys):
    docs = tmp_path / "src"
    docs.mkdir()
    (docs / "broken.py").write_text("def broken(:\n")
    (docs / "ok.md").write_text("# Ok\n\nFine.\n")
    chunks, _ = collect(argparse.Namespace(docs=[docs], repo_root=None), Limits())
    assert [c.source_path for c in chunks] == ["src/ok.md"]
    assert "skipped src/broken.py" in capsys.readouterr().err
