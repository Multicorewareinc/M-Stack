"""Cuts documents into chunks, by type, for any corpus.

Each type is cut along its own structure first -- a function, a section, a
key, a row, a slide, a page -- and only a unit too long for one chunk is
split further, into overlapping parts (base.py). Every chunk says where it
sits, and credentials are redacted from all of them.

  code          .py (parsed); about forty other languages by top-level block;
                Dockerfile, Makefile and similar by name; notebooks (.ipynb)
  structured    .json .jsonl/.ndjson .yaml/.yml .toml .ini .cfg .conf
                .properties .csv .tsv .xlsx .xml
  documents     .md .html/.htm .docx .odt .pptx .epub .pdf .eml .txt .rst
                .log, and any other file that is plain text

What can't be read without a model -- images and scanned pages (OCR),
audio and video (speech-to-text) -- or without a converter -- legacy
.doc/.xls/.ppt -- is skipped, with the reason (unsupported_reason).

Nothing here knows which corpus it is cutting; corpus.py labels the
Multistack source on top of it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path

from .base import (
    DEFAULT_LIMITS,
    REDACTED,
    Chunk,
    Limits,
    estimate_tokens,
    first_sentence,
    is_secret_key,
    redact,
    redact_settings,
    split_lines,
)
from .code import chunk_code, chunk_python
from .documents import (
    UnreadableDocument,
    chunk_email,
    chunk_pdf_pages,
    chunk_slides,
    chunk_text,
    docx_to_markdown,
    epub_to_markdown,
    odt_to_markdown,
    pdf_pages,
    pptx_slides,
    xlsx_sheets,
)
from .markup import chunk_html, chunk_markdown, chunk_xml, html_to_markdown, markdown_parts
from .structured import (
    chunk_csv,
    chunk_json,
    chunk_json_lines,
    chunk_sections,
    chunk_yaml,
    notebook_to_markdown,
    table_rows,
)

__all__ = [
    "DEFAULT_LIMITS",
    "REDACTED",
    "Chunk",
    "Limits",
    "UnreadableDocument",
    "chunk_code",
    "chunk_csv",
    "chunk_document",
    "chunk_html",
    "chunk_json",
    "chunk_markdown",
    "chunk_pdf_pages",
    "chunk_python",
    "chunk_text",
    "chunk_xml",
    "chunk_yaml",
    "document_files",
    "estimate_tokens",
    "first_sentence",
    "html_to_markdown",
    "is_secret_key",
    "markdown_parts",
    "redact",
    "redact_settings",
    "skipped_files",
    "split_lines",
    "unsupported_reason",
]

TextChunker = Callable[..., Iterator[Chunk]]

CODE_SUFFIXES = frozenset(
    {
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".vue",
        ".svelte",
        ".go",
        ".java",
        ".kt",
        ".kts",
        ".scala",
        ".groovy",
        ".gradle",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".cxx",
        ".hpp",
        ".hh",
        ".cs",
        ".m",
        ".mm",
        ".rs",
        ".swift",
        ".dart",
        ".rb",
        ".php",
        ".pl",
        ".lua",
        ".r",
        ".jl",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".ps1",
        ".bat",
        ".cmd",
        ".sql",
        ".tf",
        ".hcl",
        ".proto",
        ".graphql",
        ".gql",
        ".css",
        ".scss",
        ".sass",
        ".less",
        ".cmake",
    }
)
CODE_NAMES = frozenset(
    {
        "Dockerfile",
        "Containerfile",
        "Makefile",
        "GNUmakefile",
        "Jenkinsfile",
        "Vagrantfile",
        "Gemfile",
        "Rakefile",
        "Procfile",
        "Tiltfile",
        "Justfile",
        "CMakeLists.txt",
    }
)

_TEXT_CHUNKERS: dict[str, TextChunker] = {
    ".py": chunk_python,
    ".pyi": chunk_python,
    ".md": chunk_markdown,
    ".markdown": chunk_markdown,
    ".mdx": chunk_markdown,
    ".html": chunk_html,
    ".htm": chunk_html,
    ".xhtml": chunk_html,
    ".xml": chunk_xml,
    ".json": chunk_json,
    ".jsonl": chunk_json_lines,
    ".ndjson": chunk_json_lines,
    ".yaml": chunk_yaml,
    ".yml": chunk_yaml,
    ".toml": chunk_sections,
    ".ini": chunk_sections,
    ".cfg": chunk_sections,
    ".conf": chunk_sections,
    ".properties": chunk_sections,
    ".csv": chunk_csv,
    ".tsv": chunk_csv,
    ".txt": chunk_text,
    ".text": chunk_text,
    ".rst": chunk_text,
    ".adoc": chunk_text,
    ".org": chunk_text,
    ".tex": chunk_text,
    ".log": chunk_text,
    **dict.fromkeys(CODE_SUFFIXES, chunk_code),
}
BINARY_SUFFIXES = frozenset({".pdf", ".docx", ".odt", ".pptx", ".xlsx", ".epub", ".eml", ".ipynb"})
SUPPORTED_SUFFIXES = frozenset({*_TEXT_CHUNKERS, *BINARY_SUFFIXES})

# Known, and not readable here: the reason is what the indexer reports.
_UNSUPPORTED = {
    "an image; reading its text needs OCR": {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".tif",
        ".tiff",
        ".webp",
        ".heic",
        ".svg",
        ".ico",
    },
    "audio or video; reading it needs speech-to-text": {
        ".mp3",
        ".wav",
        ".m4a",
        ".flac",
        ".ogg",
        ".mp4",
        ".mov",
        ".avi",
        ".mkv",
        ".webm",
    },
    "a legacy Office format; convert it to .docx, .xlsx or .pptx first": {
        ".doc",
        ".xls",
        ".ppt",
        ".rtf",
        ".msg",
    },
    "an archive; unpack it into the folder first": {".zip", ".tar", ".gz", ".tgz", ".7z", ".rar"},
    "a binary file": {
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".bin",
        ".o",
        ".a",
        ".class",
        ".jar",
        ".pyc",
        ".wasm",
    },
    "a database file": {".db", ".sqlite", ".sqlite3", ".parquet", ".avro", ".pkl"},
}

# Never read, whatever directory they are in.
_SKIPPED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        "dist",
        "build",
        "target",
        "vendor",
        ".terraform",
    }
)
_SKIPPED_NAMES = re.compile(
    r"^(\.env.*|.*\.(pem|key|p12|pfx|crt|jks|keystore)|id_[a-z0-9]+"
    # credentials.json, secrets.yaml -- data, where credentials.py is code
    r"|(credentials|secrets?)(\.(json|ya?ml|toml|ini|env|txt|csv|xml|conf|cfg|properties))?"
    r"|.*\.lock|package-lock\.json|.*\.min\.(js|css)|.*\.map)$",
    re.I,
)
MAX_FILE_BYTES = 20 * 1024 * 1024
# Bytes read to decide whether a file of unknown type is text.
_SNIFF_BYTES = 8192


def _is_text(path: Path) -> bool:
    with path.open("rb") as f:
        head = f.read(_SNIFF_BYTES)
    if b"\0" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as e:
        return e.start >= len(head) - 4  # a character cut at the end of the sample
    return True


def unsupported_reason(path: Path) -> str | None:
    """Why a file is not read, or None when it is."""
    if _SKIPPED_NAMES.match(path.name):
        return "named like a credential, a lock file or build output"
    suffix = path.suffix.lower()
    if suffix in SUPPORTED_SUFFIXES or path.name in CODE_NAMES:
        return None
    for reason, suffixes in _UNSUPPORTED.items():
        if suffix in suffixes:
            return reason
    return None if _is_text(path) else "a binary file of an unknown type"


def chunk_document(
    path: Path, rel: str, *, capability: str | None = None, limits: Limits = DEFAULT_LIMITS
) -> list[Chunk]:
    """Every chunk of the file at `path`, labelled `rel`, with credentials
    redacted. Raises ValueError for a file nothing here reads (with the
    reason) or one that can't be read, and SyntaxError for Python that
    doesn't parse."""
    reason = unsupported_reason(path)
    if reason:
        raise ValueError(f"{rel}: {reason}")
    suffix = path.suffix.lower()
    chunks: Iterator[Chunk]
    if suffix == ".pdf":
        chunks = chunk_pdf_pages(pdf_pages(path), rel, capability=capability, limits=limits)
    elif suffix == ".docx":
        chunks = chunk_markdown(docx_to_markdown(path), rel, capability=capability, limits=limits)
    elif suffix == ".odt":
        chunks = chunk_markdown(odt_to_markdown(path), rel, capability=capability, limits=limits)
    elif suffix == ".epub":
        chunks = chunk_markdown(epub_to_markdown(path), rel, capability=capability, limits=limits)
    elif suffix == ".pptx":
        chunks = chunk_slides(pptx_slides(path), rel, capability=capability, limits=limits)
    elif suffix == ".xlsx":
        chunks = (
            c
            for name, rows in xlsx_sheets(path)
            for c in table_rows(
                rows, rel, where=f"sheet '{name}' in ", capability=capability, limits=limits
            )
        )
    elif suffix == ".eml":
        chunks = chunk_email(path.read_bytes(), rel, capability=capability, limits=limits)
    elif suffix == ".ipynb":
        chunks = chunk_markdown(
            notebook_to_markdown(path.read_text(encoding="utf-8")),
            rel,
            capability=capability,
            limits=limits,
        )
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
        chunker = _TEXT_CHUNKERS.get(suffix) or (
            chunk_code if path.name in CODE_NAMES else chunk_text
        )
        chunks = chunker(text, rel, capability=capability, limits=limits)
    # Source code names its secrets without holding them; everything else
    # -- config, data, documents -- may hold them in `key: value` form.
    clean = (
        redact
        if suffix in (".py", ".pyi", *CODE_SUFFIXES) or path.name in CODE_NAMES
        else redact_settings
    )
    return [replace(c, text=clean(c.text)) for c in chunks if c.text.strip()]


def document_files(root: Path) -> list[Path]:
    """Every file under `root` that chunk_document reads, leaving out
    dependency, build and hidden directories, and anything named like a
    credential (.env files, keys)."""
    return sorted(
        f
        for f in root.rglob("*")
        if f.is_file()
        and not any(p in _SKIPPED_DIRS or p.startswith(".") for p in f.relative_to(root).parts[:-1])
        and f.stat().st_size <= MAX_FILE_BYTES
        and unsupported_reason(f) is None
    )


def skipped_files(root: Path) -> dict[str, list[Path]]:
    """The files under `root` that document_files leaves out for their type,
    by reason, so an indexing run can say what it didn't read."""
    skipped: dict[str, list[Path]] = {}
    for f in root.rglob("*"):
        if not f.is_file() or any(
            p in _SKIPPED_DIRS or p.startswith(".") for p in f.relative_to(root).parts[:-1]
        ):
            continue
        reason = "larger than 20 MB" if f.stat().st_size > MAX_FILE_BYTES else unsupported_reason(f)
        if reason:
            skipped.setdefault(reason, []).append(f)
    return skipped
