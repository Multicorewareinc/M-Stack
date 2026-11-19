"""Source code: Python through its own parser, any other language by its
top-level blocks."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator

from .base import (
    DEFAULT_LIMITS,
    Chunk,
    Limits,
    block,
    estimate_tokens,
    first_sentence,
    pack_blocks,
    paragraphs,
    parts,
)


def _with_leading_comment(lines: list[str], node: ast.stmt) -> tuple[int, int]:
    """The node's lines, extended up over the `#` comment block directly
    above it: a field's comment is often the only record of what it is
    for, and comments are not part of the syntax tree."""
    top = node.lineno - 1
    while top > 0 and lines[top - 1].strip().startswith("#"):
        top -= 1
    return top + 1, node.end_lineno or node.lineno


def _definition(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> tuple[int, int]:
    """From the first decorator: `@property`, a validator's field and a
    route's path are written above the `def` line."""
    start = min([d.lineno for d in node.decorator_list] + [node.lineno])
    return start, node.end_lineno or node.lineno


def _describe_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef, owner: str | None, outer: str | None
) -> str:
    if owner:
        decorators = {ast.unparse(d).split("(")[0].split(".")[-1] for d in node.decorator_list}
        kind = "property" if {"property", "cached_property"} & decorators else "method"
        return f"{kind} '{node.name}' of class {owner}"
    if outer:
        return f"function '{node.name}' inside function '{outer}'"
    return f"function '{node.name}'"


def chunk_python(
    text: str, rel: str, *, capability: str | None = None, limits: Limits = DEFAULT_LIMITS
) -> Iterator[Chunk]:
    """The module docstring and each top-level constant; for every class,
    at any depth, an overview (signature and docstring), each field with
    the comment above it, and each method; every function, and every
    function defined inside one, whole.

    A nested function is also inside its parent's chunk, which reads as
    the source does; its own chunk lets a question about it find it
    without the rest of the parent. A unit longer than `limits` is split
    into overlapping parts."""
    tree = ast.parse(text)
    lines = text.splitlines()

    def chunk(
        id_: str, kind: str, context: str, start: int, end: int, symbol: str | None = None
    ) -> Iterator[Chunk]:
        base = Chunk(id_, "", context, capability, rel, start, end, kind, symbol)
        yield from block(base, lines[start - 1 : end], start, limits)

    module_doc = ast.get_docstring(tree)
    if module_doc:
        first = tree.body[0]
        base = Chunk(
            f"{rel}:1:docstring",
            module_doc,
            f"module overview for {rel}",
            capability,
            rel,
            1,
            first.end_lineno or 1,
            "section",
        )
        if estimate_tokens(module_doc) <= limits.max_tokens:
            yield base
        else:
            yield from parts(base, pack_blocks(paragraphs(module_doc, first.lineno), limits))
    # A constant's name rarely says what it is for ("DEFAULT_PLANS" is the
    # plans a first install creates); the module docstring's first sentence
    # usually does, and a question worded like that sentence should find it.
    about = f" ({first_sentence(module_doc)})" if module_doc else ""
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) for t in node.targets):
            name = ", ".join(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
        else:
            continue
        start, end = _with_leading_comment(lines, node)
        yield from chunk(
            f"{rel}:{node.lineno}:constant",
            "field",
            f"constant {name}, in {rel}{about}",
            start,
            end,
        )

    def visit(body: list[ast.stmt], owner: str | None, outer: str | None) -> Iterator[Chunk]:
        for node in body:
            if isinstance(node, ast.ClassDef):
                name = f"{owner}.{node.name}" if owner else node.name
                doc = ast.get_docstring(node)
                start, _ = _definition(node)
                end = (node.body[0].end_lineno or node.lineno) if doc else node.lineno
                overview = f'class {node.name}:\n    """{doc}"""' if doc else f"class {node.name}:"
                base = Chunk(
                    f"{rel}:{node.lineno}:class",
                    overview,
                    f"class {name}, in {rel}",
                    capability,
                    rel,
                    start,
                    end,
                    "class",
                )
                if estimate_tokens(overview) <= limits.max_tokens:
                    yield base
                else:
                    yield from parts(base, pack_blocks(paragraphs(overview, start), limits))
                for item in node.body:
                    if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                        start, end = _with_leading_comment(lines, item)
                        yield from chunk(
                            f"{rel}:{item.lineno}:field",
                            "field",
                            f"field '{item.target.id}' of class {name}, in {rel}",
                            start,
                            end,
                            f"{name}.{item.target.id}",
                        )
                yield from visit(node.body, name, None)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start, end = _definition(node)
                yield from chunk(
                    f"{rel}:{node.lineno}:{'method' if owner else 'function'}",
                    "function",
                    f"{_describe_function(node, owner, outer)}, in {rel}",
                    start,
                    end,
                )
                yield from visit(node.body, None, node.name)
            elif isinstance(node, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                # Definitions under `if TYPE_CHECKING:` or a try/except import guard.
                for field in ("body", "orelse", "finalbody"):
                    yield from visit(getattr(node, field, []) or [], owner, outer)
                for handler in getattr(node, "handlers", []):
                    yield from visit(handler.body, owner, outer)

    yield from visit(tree.body, None, None)


# ---- any other language

# String literals and line comments, removed before counting brackets so a
# "{" in a string or a comment doesn't open a block.
_STRINGS = re.compile(r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`[^`]*`")
_LINE_COMMENT = re.compile(r"(?:^|\s)(?://|#|--).*$")
_COMMENT_ONLY = re.compile(r"^\s*(?://|#|--|/\*|\*|<!--)")
# A unit this small -- a statement or two, an import, a constant -- is
# joined with its neighbours, so a run of imports is one chunk, not one per
# line. A function or type, however short, keeps a chunk of its own.
_SMALL_UNIT_TOKENS = 48
_SMALL_UNIT_LINES = 2


def _bracket_depth(line: str, in_block_comment: bool) -> tuple[int, bool]:
    """How much `line` opens or closes brackets, outside strings and
    comments, and whether it ends inside a /* block comment */."""
    out: list[str] = []
    i = 0
    while i < len(line):
        if in_block_comment:
            end = line.find("*/", i)
            if end == -1:
                return _count(out), True
            i, in_block_comment = end + 2, False
            continue
        start = line.find("/*", i)
        if start == -1:
            out.append(line[i:])
            break
        out.append(line[i:start])
        i, in_block_comment = start + 2, True
    return _count(out), in_block_comment


def _count(pieces: list[str]) -> int:
    code = _LINE_COMMENT.sub("", _STRINGS.sub("", "".join(pieces)))
    return sum(code.count(c) for c in "{([") - sum(code.count(c) for c in "})]")


def _signature(lines: list[str]) -> str:
    """The first line of a unit that is neither a comment nor blank: in
    most languages, the declaration."""
    for line in lines:
        if line.strip() and not _COMMENT_ONLY.match(line):
            return line.strip()[:100]
    return lines[0].strip()[:100]


def code_units(lines: list[str]) -> list[tuple[int, int]]:
    """(first, last) line numbers of each top-level unit: a declaration,
    statement or block, with the comment lines directly above it. A unit
    ends at a blank line once every bracket it opened is closed, which in
    braced languages is the end of a function or type, and in others
    (shell, SQL, Ruby, Lua) the blank line people leave between them."""
    units: list[tuple[int, int]] = []
    depth, in_comment, start = 0, False, None
    for n, line in enumerate(lines, start=1):
        if not line.strip():
            if start is not None and depth <= 0 and not in_comment:
                units.append((start, n - 1))
                start, depth = None, 0
            continue
        if start is None:
            start = n
        change, in_comment = _bracket_depth(line, in_comment)
        depth += change
    if start is not None:
        units.append((start, len(lines)))
    return units


def chunk_code(
    text: str, rel: str, *, capability: str | None = None, limits: Limits = DEFAULT_LIMITS
) -> Iterator[Chunk]:
    """Code in a language without a parser here -- JavaScript, Go, Java,
    C, Rust, SQL, shell, Terraform and the rest -- cut at its top-level
    units (see code_units). Each unit is a chunk named by its first line;
    small neighbouring units are joined; a long one is split into
    overlapping parts. Not a parser: it reads brackets and blank lines, so
    an unusual layout can join two units or split one, never lose a line."""
    lines = text.splitlines()
    small: list[tuple[int, int, str]] = []

    def flush_small() -> Iterator[Chunk]:
        for start, end, body in pack_blocks(small, Limits(limits.max_tokens, 0)):
            yield Chunk(
                f"{rel}:{start}:code",
                body,
                f"code in {rel}, lines {start}-{end}",
                capability,
                rel,
                start,
                end,
                "code",
            )
        small.clear()

    for start, end in code_units(lines):
        body = "\n".join(lines[start - 1 : end])
        statements = [
            line
            for line in lines[start - 1 : end]
            if line.strip() and not _COMMENT_ONLY.match(line)
        ]
        if estimate_tokens(body) < _SMALL_UNIT_TOKENS and len(statements) <= _SMALL_UNIT_LINES:
            small.append((start, end, body))
            continue
        yield from flush_small()
        base = Chunk(
            f"{rel}:{start}:code",
            "",
            f"`{_signature(lines[start - 1 : end])}` in {rel}",
            capability,
            rel,
            start,
            end,
            "code",
        )
        yield from block(base, lines[start - 1 : end], start, limits)
    yield from flush_small()
