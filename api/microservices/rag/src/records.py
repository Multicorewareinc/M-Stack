"""A chunk as it is stored, and how one stored vector is identified."""

from __future__ import annotations

import hashlib
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from chunking import Chunk
from corpus import origin

_POINT_NAMESPACE = uuid.UUID("6f0a1c3e-52d4-4b7e-9a68-3c1d2e4f5a60")


def content_hash(embed_text: str, model: str) -> str:
    """Identifies a vector: the same text under the same model always
    gives the same one. Keyed on content rather than on the chunk id
    because an edit near the top of a file shifts the line number, and so
    the id, of every chunk below it."""
    return hashlib.sha256(f"{model}\0{embed_text}".encode()).hexdigest()


def point_id(chunk_id: str) -> str:
    """The store's id for a chunk: stable, so re-indexing overwrites."""
    return str(uuid.uuid5(_POINT_NAMESPACE, chunk_id))


@dataclass(frozen=True)
class Record:
    id: str
    text: str
    context: str
    capability: str | None
    source_path: str
    start_line: int
    end_line: int
    kind: str
    content_hash: str
    symbol: str | None = None
    origin: str = "other"

    @property
    def embed_text(self) -> str:
        return f"{self.context}\n\n{self.text}"

    @classmethod
    def from_chunk(cls, chunk: Chunk, model: str) -> Record:
        return cls(
            id=chunk.id,
            text=chunk.text,
            context=chunk.context,
            capability=chunk.capability,
            source_path=chunk.source_path,
            start_line=chunk.start_line,
            end_line=chunk.end_line,
            kind=chunk.kind,
            content_hash=content_hash(chunk.embed_text, model),
            symbol=chunk.symbol,
            origin=origin(chunk.source_path),
        )

    def payload(self) -> dict:
        return {
            "chunk_id": self.id,
            "text": self.text,
            "context": self.context,
            "capability": self.capability or "",
            "source_path": self.source_path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "kind": self.kind,
            "content_hash": self.content_hash,
            "symbol": self.symbol or "",
            # The bare field name, for an exact lookup without the class.
            "field_name": (self.symbol or "").rpartition(".")[2],
            "origin": self.origin,
        }


def git_commit(repo_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


# The directories corpus_files() reads.
INDEXED_PATHS = ("multistack", "api/microservices", "agentic/backend/src/skills")


def source_is_modified(repo_root: Path) -> bool | None:
    """Whether the indexed source in the working tree differs from HEAD,
    counting new files git doesn't track yet. An index built from such a
    tree holds more than its commit says. None when git can't tell."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--", *INDEXED_PATHS],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return bool(result.stdout.strip())
