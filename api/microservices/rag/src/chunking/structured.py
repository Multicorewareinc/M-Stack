"""Structured data: JSON, JSON Lines, YAML, TOML and INI-style config,
tables (CSV, TSV, and spreadsheets through documents.py), and Jupyter
notebooks.

Each is cut along its own shape -- keys, sections, rows -- and every chunk
says where it sits: `key spec.template.containers[0] of deploy.yaml`,
`rows 10-40 of sales.csv (columns: region, month, revenue)`. A value whose
key names a credential is redacted, whatever the value looks like.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Iterator, Sequence
from itertools import pairwise
from typing import Any

from .base import (
    DEFAULT_LIMITS,
    REDACTED,
    Chunk,
    Limits,
    estimate_tokens,
    is_secret_key,
    pack_blocks,
    paragraphs,
    parts,
    split_lines,
)

# Siblings listed by name in a joined chunk's description, before "…".
_NAMES_SHOWN = 6


def _names(names: Sequence[str]) -> str:
    shown = ", ".join(names[:_NAMES_SHOWN])
    return shown + (", …" if len(names) > _NAMES_SHOWN else "")


# ---- JSON


def _without_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: (
                REDACTED
                if is_secret_key(str(k)) and v not in (None, "", True, False)
                else _without_secrets(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_without_secrets(v) for v in value]
    return value


def _dump(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


def chunk_json(
    text: str, rel: str, *, capability: str | None = None, limits: Limits = DEFAULT_LIMITS
) -> Iterator[Chunk]:
    """From the top down: a value that fits is one chunk, labelled with its
    path ($.spec.replicas, $.items[3]); one that doesn't is split into its
    keys or items, small neighbours joined. Line numbers span the whole
    file, as a parsed document keeps none."""
    data = _without_secrets(json.loads(text))
    last = max(1, text.count("\n") + 1)

    def chunk(id_: str, body: str, context: str) -> Iterator[Chunk]:
        base = Chunk(id_, "", context, capability, rel, 1, last, "field")
        yield from parts(base, split_lines(body.splitlines() or [""], 1, limits))

    def walk(value: Any, path: str) -> Iterator[Chunk]:
        body = _dump(value)
        if (
            estimate_tokens(body) <= limits.max_tokens
            or not value
            or not isinstance(value, (dict, list))
        ):
            yield from chunk(f"{rel}:{path}", body, f"{path} of {rel}")
            return
        entries = (
            [(f"{path}.{k}", k, v) for k, v in value.items()]
            if isinstance(value, dict)
            else [(f"{path}[{i}]", i, v) for i, v in enumerate(value)]
        )
        group: list[tuple[str, Any, Any]] = []

        def flush() -> Iterator[Chunk]:
            if not group:
                return
            if isinstance(value, dict):
                body = _dump({k: v for _, k, v in group})
                context = f"keys {_names([str(k) for _, k, _ in group])} of {path} in {rel}"
            else:
                body = _dump([v for _, _, v in group])
                first, end = group[0][1], group[-1][1]
                context = f"items {first}-{end} of {path} in {rel}"
            yield from chunk(f"{rel}:{group[0][0]}:group", body, context)
            group.clear()

        for child_path, key, child in entries:
            if estimate_tokens(_dump(child)) > limits.max_tokens:
                yield from flush()
                yield from walk(child, child_path)
                continue
            candidate = [*group, (child_path, key, child)]
            joined = (
                _dump({k: v for _, k, v in candidate})
                if isinstance(value, dict)
                else _dump([v for _, _, v in candidate])
            )
            if group and estimate_tokens(joined) > limits.max_tokens:
                yield from flush()
            group.append((child_path, key, child))
        yield from flush()

    yield from walk(data, "$")


def chunk_json_lines(
    text: str, rel: str, *, capability: str | None = None, limits: Limits = DEFAULT_LIMITS
) -> Iterator[Chunk]:
    """One record per line, records joined up to the size limit, numbered
    by line. A line that isn't JSON is kept as it is."""
    blocks = []
    for n, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.dumps(_without_secrets(json.loads(line)), ensure_ascii=False)
        except ValueError:
            record = line
        blocks.append((n, n, record))
    for start, end, body in pack_blocks(blocks, Limits(limits.max_tokens, 0)):
        yield Chunk(
            f"{rel}:{start}:records",
            body,
            f"records on lines {start}-{end} of {rel}",
            capability,
            rel,
            start,
            end,
            "field",
        )


# ---- YAML, read by its indentation so comments and line numbers survive

_YAML_KEY = re.compile(r"^\s*(-\s+)?([^\s#:][^:#]*?)\s*:(\s|$)")
_YAML_ITEM = re.compile(r"^\s*-(\s|$)")
_DOCUMENT_BREAK = re.compile(r"^---\s*(#.*)?$")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_content(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _entries(lines: list[str], first: int, last: int) -> list[tuple[int, int, int]]:
    """The entries at the outermost indentation of lines first..last
    (1-based) as (top, end, key line): each from its key line -- or the
    comment lines just above it -- to the line before the next entry."""
    content = [n for n in range(first, last + 1) if _is_content(lines[n - 1])]
    if not content:
        return []
    level = min(_indent(lines[n - 1]) for n in content)
    starts = [n for n in content if _indent(lines[n - 1]) == level]
    tops = []
    for i, start in enumerate(starts):
        if i == 0:
            tops.append(first)
            continue
        top = start
        while top - 1 > starts[i - 1] and lines[top - 2].strip().startswith("#"):
            top -= 1
        tops.append(top)
    return [
        (top, (tops[i + 1] - 1) if i + 1 < len(tops) else last, starts[i])
        for i, top in enumerate(tops)
    ]


def _entry_name(line: str, items: dict[str, int], parent: str) -> str:
    """`parent.key` for a key line, `parent[n]` for the n-th list item."""
    key = _YAML_KEY.match(line)
    if key and not key.group(1):
        name = key.group(2).strip("\"'")
        return f"{parent}.{name}" if parent else name
    if _YAML_ITEM.match(line):
        index = items.get(parent, 0)
        items[parent] = index + 1
        return f"{parent}[{index}]"
    return f"{parent}.{line.strip()[:30]}" if parent else line.strip()[:30]


def chunk_yaml(
    text: str, rel: str, *, capability: str | None = None, limits: Limits = DEFAULT_LIMITS
) -> Iterator[Chunk]:
    """Key by key, read from the indentation rather than parsed, so the
    comments that explain a value -- often the only explanation -- stay
    with it, and so do line numbers; a Helm template, which isn't valid
    YAML, reads the same way. An entry that fits is one chunk, labelled
    with its key path (spec.template.containers[0]); one that doesn't is
    split into the entries under it, small siblings joined."""
    lines = text.splitlines()
    breaks = [n for n, line in enumerate(lines, start=1) if _DOCUMENT_BREAK.match(line)]
    bounds = [0, *breaks, len(lines) + 1]
    for doc, (before, after) in enumerate(pairwise(bounds), start=1):
        first, last = before + 1, after - 1
        if first > last:
            continue
        prefix = f"document {doc} " if breaks else ""
        yield from _yaml_block(
            lines, _entries(lines, first, last), "", prefix, rel, capability, limits
        )


def _yaml_block(
    lines: list[str],
    entries: list[tuple[int, int, int]],
    path: str,
    prefix: str,
    rel: str,
    capability: str | None,
    limits: Limits,
) -> Iterator[Chunk]:
    items: dict[str, int] = {}
    group: list[tuple[int, int, str]] = []

    def text_of(a: int, b: int) -> str:
        return "\n".join(lines[a - 1 : b]).rstrip()

    def flush() -> Iterator[Chunk]:
        if not group:
            return
        a, b = group[0][0], group[-1][1]
        names = [name.rsplit(".", 1)[-1] for _, _, name in group]
        where = f"under {path}" if path else "at the top level"
        context = (
            f"key {group[0][2]} of {prefix}{rel}"
            if len(group) == 1
            else f"keys {_names(names)} {where} of {prefix}{rel}"
        )
        yield Chunk(f"{rel}:{a}:yaml", text_of(a, b), context, capability, rel, a, b, "field")
        group.clear()

    for top, end, key_line in entries:
        name = _entry_name(lines[key_line - 1], items, path)
        body = text_of(top, end)
        if not body.strip():
            continue
        if estimate_tokens(body) > limits.max_tokens:
            yield from flush()
            inner = _entries(lines, key_line + 1, end)
            if inner:
                # The key line, and its comments, open the first entry under it.
                inner[0] = (top, inner[0][1], inner[0][2])
                yield from _yaml_block(lines, inner, name, prefix, rel, capability, limits)
            else:
                base = Chunk(
                    f"{rel}:{top}:yaml",
                    "",
                    f"key {name} of {prefix}{rel}",
                    capability,
                    rel,
                    top,
                    end,
                    "field",
                )
                yield from parts(base, split_lines(lines[top - 1 : end], top, limits))
            continue
        if group and estimate_tokens(text_of(group[0][0], end)) > limits.max_tokens:
            yield from flush()
        group.append((top, end, name))
    yield from flush()


# ---- TOML, INI, .cfg, .conf, .properties

_SECTION = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(?:[#;].*)?$")


def chunk_sections(
    text: str, rel: str, *, capability: str | None = None, limits: Limits = DEFAULT_LIMITS
) -> Iterator[Chunk]:
    """One chunk per `[section]` (TOML tables included), with the comments
    above its header; settings before the first header are the section
    `top`. A long section is split between its blank-line groups, with
    overlap."""
    lines = text.splitlines()
    heads = [n for n, line in enumerate(lines, start=1) if _SECTION.match(line)]
    starts: list[tuple[int, int | None]] = [(1, None)]
    for head_line in heads:
        above = head_line
        while above - 1 >= 1 and lines[above - 2].strip().startswith(("#", ";")):
            above -= 1
        starts.append((above, head_line))
    bounds = starts
    for i, (top, head) in enumerate(bounds):
        end = bounds[i + 1][0] - 1 if i + 1 < len(bounds) else len(lines)
        if top > end or not any(line.strip() for line in lines[top - 1 : end]):
            continue
        name = _SECTION.match(lines[head - 1]).group(1) if head else "top"  # type: ignore[union-attr]
        base = Chunk(
            f"{rel}:{top}:section",
            "",
            f"section [{name}] of {rel}",
            capability,
            rel,
            top,
            end,
            "section",
        )
        body = "\n".join(lines[top - 1 : end])
        yield from parts(base, pack_blocks(paragraphs(body, top), limits))


# ---- tables


def table_rows(
    rows: Sequence[Sequence[str]],
    rel: str,
    *,
    first_row: int = 1,
    where: str = "",
    capability: str | None = None,
    limits: Limits = DEFAULT_LIMITS,
) -> Iterator[Chunk]:
    """Rows of a table whose first row is its header, each written as
    `column: value` pairs so a row reads on its own, joined up to the
    size limit. A column whose header names a credential is redacted.
    `first_row` is the header's row number, `where` names the sheet."""
    if not rows:
        return
    header = [h.strip() or f"column {i + 1}" for i, h in enumerate(rows[0])]
    secret = {i for i, h in enumerate(header) if is_secret_key(h)}
    blocks = []
    for offset, row in enumerate(rows[1:], start=1):
        cells = [
            f"{header[i] if i < len(header) else f'column {i + 1}'}: "
            f"{REDACTED if i in secret and value.strip() else ' '.join(value.split())}"
            for i, value in enumerate(row)
            if value.strip()
        ]
        if cells:
            n = first_row + offset
            blocks.append((n, n, "; ".join(cells)))
    columns = _names(header)
    for start, end, body in pack_blocks(blocks, Limits(limits.max_tokens, 0)):
        yield Chunk(
            f"{rel}:{where}:{start}:rows",
            body.replace("\n\n", "\n"),
            f"rows {start}-{end} of {where}{rel} (columns: {columns})",
            capability,
            rel,
            start,
            end,
            "table",
        )


def chunk_csv(
    text: str, rel: str, *, capability: str | None = None, limits: Limits = DEFAULT_LIMITS
) -> Iterator[Chunk]:
    """A delimited table: its delimiter is detected from a sample (comma,
    tab, semicolon or pipe), and its first row is taken as the header."""
    sample = text[:4096]
    try:
        dialect: Any = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel_tab if rel.endswith(".tsv") else csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    yield from table_rows(rows, rel, capability=capability, limits=limits)


# ---- Jupyter notebooks


def notebook_to_markdown(text: str) -> str:
    """Markdown cells as they are, code cells as fenced blocks in the
    notebook's language. Outputs are left out: they are results, often
    large, and rarely what a question is about."""
    notebook = json.loads(text)
    language = (
        notebook.get("metadata", {}).get("kernelspec", {}).get("language")
        or notebook.get("metadata", {}).get("language_info", {}).get("name")
        or ""
    )
    out = []
    for cell in notebook.get("cells", []):
        source = cell.get("source", "")
        source = "".join(source) if isinstance(source, list) else source
        if not source.strip():
            continue
        if cell.get("cell_type") == "code":
            out.append(f"```{language}\n{source.rstrip()}\n```")
        else:
            out.append(source.rstrip())
    return "\n\n".join(out) + "\n"
