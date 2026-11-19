"""What every chunker shares: the Chunk, size limits, splitting a unit
that is too long into overlapping parts, and redacting credentials."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Chunk:
    """One piece, ready to be embedded. `context` is prepended to `text`
    before embedding: a bare line such as `replicas: int = 2` says nothing
    about which component it belongs to, so each chunk carries a line
    saying where it sits."""

    id: str  # stable: source_path + start_line, never a random id
    text: str  # the chunk's own content
    context: str  # the one line of "where this sits" prepended before embedding
    capability: str | None  # a label to narrow or boost a search by, or None
    source_path: str  # relative to the corpus root
    start_line: int  # pages, for a PDF
    end_line: int
    kind: str  # "field" | "class" | "function" | "section" | "skill" | "rule" | "text"
    # | "skill_intent" | "dependency" | "tool_mapping" | "skill_counterexample"
    # -- all four are synthetic (hand-written from DEPENDS_ON/REASON/
    # TOOL_FOR_SKILL/FALSE_MATCHES, not cut from source). "dependency" and
    # "tool_mapping" are always read by exact tag, never searched.
    # "skill_counterexample" is the opposite: it's ONLY ever searched by
    # embedding (never read by tag), to subtract from "skill_intent"'s own
    # score -- see corpus.py's *_chunks() functions and service.py's
    # rank_skills().
    # "Class.field" for a class field, so a lookup by field name is exact.
    symbol: str | None = None

    @property
    def embed_text(self) -> str:
        return f"{self.context}\n\n{self.text}"

    def __str__(self) -> str:  # for the inspection script
        return (
            f"[{self.kind}] {self.source_path}:{self.start_line}-{self.end_line}"
            f"  ({self.capability or '-'})\n"
            f"  context: {self.context}\n"
            f"  {'-' * 60}\n" + "\n".join(f"  {line}" for line in self.text.splitlines())
        )


@dataclass(frozen=True)
class Limits:
    """How long a chunk may be, and how much consecutive parts of a split
    unit share, so a sentence or statement cut at a boundary is whole in
    one of them. Tokens are estimated at four characters each."""

    max_tokens: int = 512
    overlap_tokens: int = 64


DEFAULT_LIMITS = Limits()


def estimate_tokens(text: str) -> int:
    return len(text) // 4 + 1


# ---- credentials

_SECRET_SHAPES = [
    re.compile(r"gsk_[A-Za-z0-9]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[abpr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
]
_WORDS = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")
_SECRET_WORDS = {"password", "passwd", "pwd", "secret", "token", "credential", "credentials"}
_SECRET_PAIRS = ({"api", "key"}, {"access", "key"}, {"private", "key"}, {"apikey"})
# A key that names where a secret is, rather than holding one.
_REFERENCE_WORDS = {
    "name",
    "ref",
    "existing",
    "file",
    "path",
    "url",
    "env",
    "header",
    "ttl",
    "length",
}


def is_secret_key(key: str) -> bool:
    """Whether a setting's name says its value is a credential: SERVICE_API_KEY,
    clientSecret, db.password -- but not max_tokens, tokenizer_url,
    NODE_TOKEN_PATH or existing_secret_name, which hold no secret."""
    words = {w.lower() for w in _WORDS.findall(key)}
    if words & _REFERENCE_WORDS:
        return False
    return bool(words & _SECRET_WORDS) or any(pair <= words for pair in _SECRET_PAIRS)


def _is_placeholder(value: str) -> bool:
    """`...`, `***`, `xxxx`, `<your-key>`, `${VAR}`, `{{ .Values.x }}`, a
    path or a URL: a value that stands in for a secret or says where one
    is, rather than being one."""
    return (
        not value.strip("." + "*" + "x" + "X" + "-")
        or value[0] in "{$<"
        or value.startswith("/")
        or "://" in value
    )


# KEY=value or key: value where the name says secret and the value looks
# generated: long, and mixing letters and digits. A reference such as
# `password = settings.db_password` doesn't match.
_SECRET_ASSIGNMENT = re.compile(
    r"(?im)^([ \t]*[\"']?)([\w.-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)[\w.-]*)"
    r"([\"']?[ \t]*[:=][ \t]*[\"']?)((?=[A-Za-z0-9+/_-]*\d)(?=[A-Za-z0-9+/_-]*[A-Za-z])[A-Za-z0-9+/_-]{16,})"
)
REDACTED = "<redacted>"


def redact(text: str) -> str:
    """`text` with anything shaped like a credential replaced. Documents
    from outside the code -- notes, exports, PDFs -- do carry real keys."""
    for pattern in _SECRET_SHAPES:
        text = pattern.sub(REDACTED, text)

    def replace_value(m: re.Match[str]) -> str:
        if not is_secret_key(m.group(2)) or _is_placeholder(m.group(4)):
            return m.group(0)
        return m.group(1) + m.group(2) + m.group(3) + REDACTED

    return _SECRET_ASSIGNMENT.sub(replace_value, text)


_SETTING = re.compile(
    r"(?m)^([ \t]*(?:-[ \t]+)?[\"']?)([A-Za-z_][\w.-]*)([\"']?[ \t]*[:=][ \t]*[\"']?)([^\s\"'#,}\]]+)"
)
_NOT_A_VALUE = {"null", "none", "~", "true", "false", "|", ">", "{", "["}


def redact_settings(text: str) -> str:
    """`redact`, and also the value of any `key: value` or `KEY=value` line
    whose key names a credential, whatever the value looks like: in a
    config file or a document, `password: hunter2` is a real password.
    Values that point elsewhere -- `{{ .Values.x }}`, `${VAR}`, `<your-key>`
    -- are kept. Not for source code, where `password = settings.password`
    is a reference."""

    def replace_value(m: re.Match[str]) -> str:
        value = m.group(4)
        if not is_secret_key(m.group(2)) or value.lower() in _NOT_A_VALUE or _is_placeholder(value):
            return m.group(0)
        return m.group(1) + m.group(2) + m.group(3) + REDACTED

    return _SETTING.sub(replace_value, redact(text))


# ---- splitting a unit that is too long


def split_lines(lines: list[str], first_line: int, limits: Limits) -> list[tuple[int, int, str]]:
    """`lines` (numbered from `first_line`) as (start, end, text) windows of
    at most `limits.max_tokens`, each starting far enough back to repeat
    about `limits.overlap_tokens` of the one before. A single line longer
    than a window is a window of its own, cut to fit."""
    if estimate_tokens("\n".join(lines)) <= limits.max_tokens:
        return [(first_line, first_line + len(lines) - 1, "\n".join(lines))]
    max_chars = limits.max_tokens * 4
    windows: list[tuple[int, int, str]] = []
    start = 0
    while start < len(lines):
        end, size = start, 0
        while end < len(lines) and (end == start or size + len(lines[end]) + 1 <= max_chars):
            size += len(lines[end]) + 1
            end += 1
        text = "\n".join(lines[start:end])[:max_chars]
        windows.append((first_line + start, first_line + end - 1, text))
        if end >= len(lines):
            break
        back, shared = end, 0
        while back - 1 > start and shared + len(lines[back - 1]) + 1 <= limits.overlap_tokens * 4:
            back -= 1
            shared += len(lines[back]) + 1
        start = back
    return windows


def tail(text: str, max_chars: int) -> str:
    """The end of `text`, at most `max_chars`, starting at a sentence, a
    line or a word rather than mid-word."""
    if len(text) <= max_chars:
        return text
    cut = text[-max_chars:]
    for separator in (". ", "\n", " "):
        at = cut.find(separator)
        if at != -1 and at + len(separator) < len(cut):
            return cut[at + len(separator) :]
    return cut


def pack_blocks(blocks: list[tuple[int, int, str]], limits: Limits) -> list[tuple[int, int, str]]:
    """Consecutive (start, end, text) blocks -- paragraphs, list items,
    code fences -- joined into windows of at most `limits.max_tokens`.
    Each window after the first opens with the last blocks of the one
    before, up to `limits.overlap_tokens`. A block too long on its own is
    split by lines."""
    pieces: list[tuple[int, int, str]] = []
    for start, end, text in blocks:
        if estimate_tokens(text) > limits.max_tokens:
            pieces.extend(split_lines(text.split("\n"), start, limits))
        else:
            pieces.append((start, end, text))

    windows: list[tuple[int, int, str]] = []
    current: list[tuple[int, int, str]] = []

    def size(items: list[tuple[int, int, str]]) -> int:
        return estimate_tokens("\n\n".join(t for _, _, t in items))

    for piece in pieces:
        if current and size([*current, piece]) > limits.max_tokens:
            windows.append((current[0][0], current[-1][1], "\n\n".join(t for _, _, t in current)))
            carried: list[tuple[int, int, str]] = []
            for item in reversed(current):
                if size([item, *carried]) > limits.overlap_tokens:
                    break
                carried.insert(0, item)
            if not carried:
                # The last block alone is more than the overlap: carry its end.
                _, last_line, last_text = current[-1]
                carried = [(last_line, last_line, tail(last_text, limits.overlap_tokens * 4))]
            current = carried if size([*carried, piece]) <= limits.max_tokens else []
        current.append(piece)
    if current:
        windows.append((current[0][0], current[-1][1], "\n\n".join(t for _, _, t in current)))
    return windows


def parts(base: Chunk, windows: list[tuple[int, int, str]]) -> Iterator[Chunk]:
    """`base` once when it fits in one window, else one chunk per window,
    each saying which part of the whole it is."""
    if len(windows) == 1:
        start, end, text = windows[0]
        yield replace(base, text=text, start_line=start, end_line=end)
        return
    for n, (start, end, text) in enumerate(windows, start=1):
        yield replace(
            base,
            id=base.id if n == 1 else f"{base.id}:p{n}",
            text=text,
            context=f"{base.context} (part {n} of {len(windows)})",
            start_line=start,
            end_line=end,
        )


def block(base: Chunk, lines: list[str], start: int, limits: Limits) -> Iterator[Chunk]:
    """A unit of source: one chunk, or overlapping parts when too long."""
    yield from parts(base, split_lines(lines, start, limits))


def first_sentence(text: str, limit: int = 160) -> str:
    """A docstring's opening sentence on one line, cut to `limit`."""
    flat = " ".join(text.split())
    end = flat.find(". ")
    sentence = flat if end == -1 else flat[: end + 1]
    return sentence if len(sentence) <= limit else sentence[: limit - 1].rstrip() + "…"


def paragraphs(text: str, first_line: int = 1) -> list[tuple[int, int, str]]:
    blocks: list[tuple[int, int, str]] = []
    current: list[str] = []
    start = first_line
    for offset, line in enumerate(text.splitlines()):
        if line.strip():
            if not current:
                start = first_line + offset
            current.append(line.rstrip())
        elif current:
            blocks.append((start, start + len(current) - 1, "\n".join(current)))
            current = []
    if current:
        blocks.append((start, start + len(current) - 1, "\n".join(current)))
    return blocks
