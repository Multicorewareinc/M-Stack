"""Markdown, and the markup converted to it: HTML and XML."""

from __future__ import annotations

import re
from collections.abc import Iterator
from html.parser import HTMLParser
from xml.etree import ElementTree

from .base import DEFAULT_LIMITS, Chunk, Limits, estimate_tokens, pack_blocks, parts, split_lines

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_LIST_ITEM = re.compile(r"^\s{0,3}(?:[-*+]|\d+[.)])\s")


def _markdown_blocks(lines: list[str], first_line: int) -> list[tuple[int, int, str]]:
    """Paragraphs, list items and fenced code blocks, each whole."""
    blocks: list[tuple[int, int, str]] = []
    current: list[str] = []
    start = first_line
    fence: str | None = None

    def flush(next_start: int) -> None:
        nonlocal current, start
        while current and not current[-1].strip():
            current.pop()
        if current:
            blocks.append((start, start + len(current) - 1, "\n".join(current)))
        current, start = [], next_start

    for offset, line in enumerate(lines):
        n = first_line + offset
        match = _FENCE.match(line)
        if fence:
            current.append(line)
            if match and match.group(1) == fence:
                fence = None
                flush(n + 1)
            continue
        if match:
            flush(n)
            fence = match.group(1)
            current.append(line)
        elif not line.strip():
            flush(n + 1)
        elif _LIST_ITEM.match(line) and current:
            # Each list item is a block; its wrapped and nested lines follow it.
            flush(n)
            current.append(line)
        else:
            if not current:
                start = n
            current.append(line)
    flush(first_line + len(lines))
    return blocks


def chunk_markdown(
    text: str, rel: str, *, capability: str | None = None, limits: Limits = DEFAULT_LIMITS
) -> Iterator[Chunk]:
    """One chunk per section: a heading and what follows it up to the next
    heading, labelled with the path of headings above it ("Deploy > Helm"),
    so a short section under a generic heading still says what it is
    about. A heading with nothing under it but deeper headings gives no
    chunk of its own; it is in its children's labels. A section too long
    for one chunk is split between paragraphs, list items and code
    fences -- never inside a fence that fits -- with overlap."""
    lines = text.splitlines()
    sections: list[tuple[list[str], int, int]] = []  # (heading path, first line, last line)
    path: list[tuple[int, str]] = []
    start, fence = 1, None
    titles: list[str] = []
    for n, line in enumerate(lines, start=1):
        match = _FENCE.match(line)
        if match:
            fence = None if fence == match.group(1) else (fence or match.group(1))
        heading = None if fence or match else _HEADING.match(line)
        if heading:
            if n > start:
                sections.append((titles, start, n - 1))
            level = len(heading.group(1))
            path = [(lvl, t) for lvl, t in path if lvl < level] + [(level, heading.group(2))]
            titles = [t for _, t in path]
            start = n
    if start <= len(lines):
        sections.append((titles, start, len(lines)))

    for titles, first, last in sections:
        body = lines[first - 1 : last]
        has_heading = bool(titles) and bool(_HEADING.match(body[0]))
        content = body[1:] if has_heading else body
        if not any(line.strip() for line in content):
            continue
        label = " > ".join(titles) if titles else "introduction"
        base = Chunk(
            f"{rel}:{first}:section",
            "",
            f"section '{label}' of {rel}",
            capability,
            rel,
            first,
            last,
            "section",
        )
        # Every part repeats the heading, so each reads on its own.
        heading_line = body[0] if has_heading else ""
        room = max(limits.max_tokens - estimate_tokens(heading_line), limits.overlap_tokens * 2)
        blocks = _markdown_blocks(content, first + 1 if has_heading else first)
        windows = pack_blocks(blocks, Limits(room, limits.overlap_tokens))
        if heading_line:
            windows = [
                (first if i == 0 else s, e, f"{heading_line}\n\n{t}")
                for i, (s, e, t) in enumerate(windows)
            ]
        yield from parts(base, windows)


def markdown_parts(
    base: Chunk, lines: list[str], first_line: int, limits: Limits = DEFAULT_LIMITS
) -> Iterator[Chunk]:
    """`base` holding `lines` of Markdown: one chunk, or overlapping parts
    split between paragraphs, list items and code fences."""
    yield from parts(base, pack_blocks(_markdown_blocks(lines, first_line), limits))


# ---- HTML

_SKIPPED_TAGS = {"script", "style", "noscript", "template", "svg", "iframe", "head"}
_BLOCK_TAGS = {
    "p",
    "div",
    "section",
    "article",
    "header",
    "footer",
    "main",
    "aside",
    "nav",
    "blockquote",
    "figure",
    "figcaption",
    "table",
    "ul",
    "ol",
    "dl",
    "dt",
    "dd",
    "form",
    "br",
    "hr",
}


class _HtmlText(HTMLParser):
    """HTML as Markdown: headings stay headings, list items become
    bullets, table rows become `a | b` lines, <pre> becomes a code fence.
    Scripts, styles and the <head> are dropped; the <title> is kept as the
    top heading when the page has no <h1>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.pre = 0
        self.title = ""
        self.in_title = False
        self.has_h1 = False
        self.cells: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self.in_title = True
        if tag in _SKIPPED_TAGS:
            self.skip += 1
        elif self.skip:
            return
        elif re.fullmatch(r"h[1-6]", tag):
            self.has_h1 |= tag == "h1"
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag == "tr":
            self.cells = []
        elif tag in ("td", "th") and self.cells is not None:
            self.cells.append("")
        elif tag == "pre":
            self.pre += 1
            self.out.append("\n\n```\n")
        elif tag in _BLOCK_TAGS:
            self.out.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if tag in _SKIPPED_TAGS:
            self.skip = max(0, self.skip - 1)
        elif self.skip:
            return
        elif re.fullmatch(r"h[1-6]", tag) or tag in _BLOCK_TAGS:
            self.out.append("\n\n")
        elif tag == "tr" and self.cells is not None:
            self.out.append("\n" + " | ".join(c.strip() for c in self.cells))
            self.cells = None
        elif tag == "pre":
            self.pre = max(0, self.pre - 1)
            self.out.append("\n```\n\n")

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data
            return
        if self.skip:
            return
        if self.cells is not None and self.cells:
            self.cells[-1] += " " + " ".join(data.split())
        elif self.pre:
            self.out.append(data)
        else:
            self.out.append(" ".join(data.split()) + (" " if data[-1:].isspace() else ""))

    def markdown(self) -> str:
        text = "".join(self.out)
        if self.title.strip() and not self.has_h1:
            text = f"# {' '.join(self.title.split())}\n\n{text}"
        lines = [line.rstrip() for line in text.splitlines()]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"


def html_to_markdown(html: str) -> str:
    parser = _HtmlText()
    parser.feed(html)
    parser.close()
    return parser.markdown()


def chunk_html(
    text: str, rel: str, *, capability: str | None = None, limits: Limits = DEFAULT_LIMITS
) -> Iterator[Chunk]:
    """By the page's headings, as Markdown is. Line numbers count lines
    of the extracted text, not of the HTML source."""
    yield from chunk_markdown(html_to_markdown(text), rel, capability=capability, limits=limits)


# ---- XML


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def chunk_xml(
    text: str, rel: str, *, capability: str | None = None, limits: Limits = DEFAULT_LIMITS
) -> Iterator[Chunk]:
    """Element by element, from the root down: an element that fits is one
    chunk, labelled with its path (/config/servers/server[2]); one that
    doesn't is split into its children, with small siblings joined. XML
    keeps no line numbers here, so every chunk spans the whole file."""
    root = ElementTree.fromstring(text)
    last = max(1, text.count("\n") + 1)

    def render(element: ElementTree.Element) -> str:
        ElementTree.indent(element)
        return ElementTree.tostring(element, encoding="unicode").strip()

    def walk(element: ElementTree.Element, path: str) -> Iterator[Chunk]:
        body = render(element)
        children = list(element)
        if estimate_tokens(body) <= limits.max_tokens or not children:
            for start, _end, piece in split_text_lines(body, limits):
                yield Chunk(
                    f"{rel}:{path}:{start}",
                    piece,
                    f"element {path} of {rel}",
                    capability,
                    rel,
                    1,
                    last,
                    "section",
                )
            return
        group: list[tuple[str, str]] = []

        def flush() -> Iterator[Chunk]:
            if not group:
                return
            if len(group) == 1:
                context = f"element {group[0][0]} of {rel}"
            else:
                shown = ", ".join(p.rsplit("/", 1)[-1] for p, _ in group[:6])
                context = f"elements {shown}{', …' if len(group) > 6 else ''} under {path} of {rel}"
            body = "\n".join(b for _, b in group)
            yield Chunk(
                f"{rel}:{group[0][0]}:group", body, context, capability, rel, 1, last, "section"
            )
            group.clear()

        totals: dict[str, int] = {}
        for child in children:
            totals[_local(child.tag)] = totals.get(_local(child.tag), 0) + 1
        counts: dict[str, int] = {}
        for child in children:
            name = _local(child.tag)
            counts[name] = counts.get(name, 0) + 1
            child_path = f"{path}/{name}" + (f"[{counts[name]}]" if totals[name] > 1 else "")
            child_body = render(child)
            if estimate_tokens(child_body) > limits.max_tokens:
                yield from flush()
                yield from walk(child, child_path)
            else:
                joined = "\n".join([*(b for _, b in group), child_body])
                if group and estimate_tokens(joined) > limits.max_tokens:
                    yield from flush()
                group.append((child_path, child_body))
        yield from flush()

    yield from walk(root, "/" + _local(root.tag))


def split_text_lines(text: str, limits: Limits) -> list[tuple[int, int, str]]:
    return split_lines(text.splitlines() or [""], 1, limits)
