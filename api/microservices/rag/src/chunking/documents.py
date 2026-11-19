"""Prose documents: plain text, PDF, office files, e-books and email."""

from __future__ import annotations

import re
import zipfile
from collections.abc import Iterator
from dataclasses import replace
from email import policy
from email.parser import BytesParser
from pathlib import Path
from xml.etree import ElementTree

from .base import DEFAULT_LIMITS, Chunk, Limits, pack_blocks, paragraphs
from .markup import html_to_markdown


def chunk_text(
    text: str, rel: str, *, capability: str | None = None, limits: Limits = DEFAULT_LIMITS
) -> Iterator[Chunk]:
    """Paragraphs joined up to `limits`, overlapping between windows."""
    base = Chunk(f"{rel}:1:text", "", f"text of {rel}", capability, rel, 1, 1, "text")
    windows = pack_blocks(paragraphs(text), limits)
    for n, (start, end, body) in enumerate(windows, start=1):
        yield replace(
            base,
            id=f"{rel}:{start}:text" if n > 1 else base.id,
            text=body,
            context=f"text of {rel}, lines {start}-{end}",
            start_line=start,
            end_line=end,
        )


def pdf_pages(path: Path) -> list[str]:
    """The text of each page. Scanned pages, with no text layer, come back
    empty: reading them needs OCR, which this does not do."""
    from pypdf import PdfReader

    return [page.extract_text() or "" for page in PdfReader(path).pages]


def chunk_pdf_pages(
    pages: list[str], rel: str, *, capability: str | None = None, limits: Limits = DEFAULT_LIMITS
) -> Iterator[Chunk]:
    """Paragraphs across pages, joined up to `limits`; a chunk's start and
    end are page numbers."""
    blocks: list[tuple[int, int, str]] = []
    for number, page in enumerate(pages, start=1):
        blocks.extend((number, number, body) for _, _, body in paragraphs(page))
    for n, (start, end, body) in enumerate(pack_blocks(blocks, limits), start=1):
        pages_label = f"page {start}" if start == end else f"pages {start}-{end}"
        yield Chunk(
            f"{rel}:{n}:pdf",
            body,
            f"{pages_label} of {rel}",
            capability,
            rel,
            start,
            end,
            "text",
        )


# ---- office files, e-books: zip archives of XML, read with the standard library

# What a single archive may unpack to. A small file that inflates past this
# is a zip bomb, not a document.
MAX_UNPACKED_BYTES = 200 * 1024 * 1024


class UnreadableDocument(ValueError):
    """A file of a supported type that can't be read: damaged, encrypted,
    or too large unpacked."""


def _archive(path: Path) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise UnreadableDocument(f"not a readable archive: {e}") from e
    if sum(info.file_size for info in archive.infolist()) > MAX_UNPACKED_BYTES:
        archive.close()
        raise UnreadableDocument("unpacks to more than the 200 MB allowed")
    if any(info.flag_bits & 0x1 for info in archive.infolist()):
        archive.close()
        raise UnreadableDocument("encrypted")
    return archive


def _xml(archive: zipfile.ZipFile, name: str) -> ElementTree.Element | None:
    try:
        return ElementTree.fromstring(archive.read(name))
    except KeyError:
        return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attr(element: ElementTree.Element, name: str) -> str:
    return next((v for k, v in element.attrib.items() if _local(k) == name), "")


def _text(element: ElementTree.Element, run: str = "t") -> str:
    return "".join(e.text or "" for e in element.iter() if _local(e.tag) == run)


def docx_to_markdown(path: Path) -> str:
    """A Word document's body in order: paragraphs styled Title or
    Heading N become headings, list paragraphs bullets, tables `a | b`
    rows."""
    with _archive(path) as archive:
        root = _xml(archive, "word/document.xml")
    if root is None:
        raise UnreadableDocument("no word/document.xml")
    body = next((e for e in root if _local(e.tag) == "body"), root)
    out = []
    for element in body:
        tag = _local(element.tag)
        if tag == "p":
            text = _text(element).strip()
            if not text:
                continue
            style = next((_attr(e, "val") for e in element.iter() if _local(e.tag) == "pStyle"), "")
            level = re.match(r"(?i)heading\s*(\d)", style)
            if style.lower() == "title":
                out.append(f"# {text}")
            elif level:
                out.append(f"{'#' * min(6, int(level.group(1)) + 1)} {text}")
            elif any(_local(e.tag) == "numPr" for e in element.iter()):
                out.append(f"- {text}")
            else:
                out.append(text)
        elif tag == "tbl":
            rows = [
                " | ".join(_text(cell).strip() for cell in row if _local(cell.tag) == "tc")
                for row in element.iter()
                if _local(row.tag) == "tr"
            ]
            out.append("\n".join(rows))
    return "\n\n".join(out) + "\n"


def odt_to_markdown(path: Path) -> str:
    """An OpenDocument text: headings by outline level, paragraphs, list
    items and table rows, in order."""
    with _archive(path) as archive:
        root = _xml(archive, "content.xml")
    if root is None:
        raise UnreadableDocument("no content.xml")
    out = []

    def text_of(element: ElementTree.Element) -> str:
        return " ".join("".join(element.itertext()).split())

    def visit(element: ElementTree.Element, in_list: bool) -> None:
        for child in element:
            tag = _local(child.tag)
            if tag == "h":
                level = int(_attr(child, "outline-level") or 1)
                out.append(f"{'#' * min(6, level)} {text_of(child)}")
            elif tag == "p":
                if text_of(child):
                    out.append(("- " if in_list else "") + text_of(child))
            elif tag == "table-row":
                out.append(" | ".join(text_of(c) for c in child if _local(c.tag) == "table-cell"))
            else:
                visit(child, in_list or tag == "list-item")

    visit(root, False)
    return "\n\n".join(line for line in out if line.strip()) + "\n"


def pptx_slides(path: Path) -> list[str]:
    """Each slide's text, its speaker notes after it, in slide order."""
    with _archive(path) as archive:
        names = sorted(
            (n for n in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
            key=lambda n: int(re.findall(r"\d+", n)[-1]),
        )
        slides = []
        for name in names:
            number = re.findall(r"\d+", name)[-1]
            root = _xml(archive, name)
            notes = _xml(archive, f"ppt/notesSlides/notesSlide{number}.xml")
            lines = (
                [" ".join(_text(p).split()) for p in root.iter() if _local(p.tag) == "p"]
                if root is not None
                else []
            )
            text = "\n".join(line for line in lines if line)
            if notes is not None:
                said = " ".join(
                    " ".join(_text(p).split()) for p in notes.iter() if _local(p.tag) == "p"
                ).strip()
                if said and not said.isdigit():
                    text += f"\nNotes: {said}"
            slides.append(text)
    return slides


def chunk_slides(
    slides: list[str], rel: str, *, capability: str | None = None, limits: Limits = DEFAULT_LIMITS
) -> Iterator[Chunk]:
    """Slides joined up to the size limit, numbered by slide."""
    blocks = [
        (n, n, f"Slide {n}\n{text}") for n, text in enumerate(slides, start=1) if text.strip()
    ]
    for start, end, body in pack_blocks(blocks, limits):
        label = f"slide {start}" if start == end else f"slides {start}-{end}"
        yield Chunk(
            f"{rel}:{start}:slides", body, f"{label} of {rel}", capability, rel, start, end, "text"
        )


def xlsx_sheets(path: Path) -> list[tuple[str, list[list[str]]]]:
    """(sheet name, rows of cell text) for each worksheet, in workbook
    order. Formulas give their last computed value, as Excel saved it."""
    with _archive(path) as archive:
        shared_root = _xml(archive, "xl/sharedStrings.xml")
        shared = (
            [_text(si) for si in shared_root if _local(si.tag) == "si"]
            if shared_root is not None
            else []
        )
        workbook = _xml(archive, "xl/workbook.xml")
        rels = _xml(archive, "xl/_rels/workbook.xml.rels")
        targets = {_attr(r, "Id"): _attr(r, "Target") for r in (rels if rels is not None else [])}
        sheets = []
        for sheet in (
            e for e in (workbook.iter() if workbook is not None else []) if _local(e.tag) == "sheet"
        ):
            target = targets.get(_attr(sheet, "id"), "")
            name = "xl/" + target.lstrip("/").removeprefix("xl/")
            root = _xml(archive, name)
            if root is None:
                continue
            rows = []
            for row in (e for e in root.iter() if _local(e.tag) == "row"):
                cells: dict[int, str] = {}
                for cell in (c for c in row if _local(c.tag) == "c"):
                    column = _column(_attr(cell, "r")) if _attr(cell, "r") else len(cells)
                    kind = _attr(cell, "t")
                    value = next((v.text or "" for v in cell if _local(v.tag) == "v"), "")
                    if kind == "s" and value.isdigit() and int(value) < len(shared):
                        value = shared[int(value)]
                    elif kind == "inlineStr":
                        value = _text(cell)
                    cells[column] = value
                if cells:
                    rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
            sheets.append((_attr(sheet, "name"), rows))
    return sheets


def _column(reference: str) -> int:
    """'C7' -> 2."""
    number = 0
    for letter in re.match(r"[A-Z]+", reference.upper()).group(0):  # type: ignore[union-attr]
        number = number * 26 + ord(letter) - 64
    return number - 1


def epub_to_markdown(path: Path) -> str:
    """An e-book's chapters in reading order, each converted from XHTML."""
    with _archive(path) as archive:
        container = _xml(archive, "META-INF/container.xml")
        opf_name = next(
            (
                _attr(e, "full-path")
                for e in (container.iter() if container is not None else [])
                if _local(e.tag) == "rootfile"
            ),
            "",
        )
        opf = _xml(archive, opf_name) if opf_name else None
        base = opf_name.rsplit("/", 1)[0] + "/" if "/" in opf_name else ""
        if opf is not None:
            manifest = {
                _attr(e, "id"): _attr(e, "href") for e in opf.iter() if _local(e.tag) == "item"
            }
            order = [
                manifest.get(_attr(e, "idref"), "")
                for e in opf.iter()
                if _local(e.tag) == "itemref"
            ]
            names = [base + href for href in order if href]
        else:
            names = sorted(n for n in archive.namelist() if n.endswith((".xhtml", ".html", ".htm")))
        chapters = []
        for name in names:
            try:
                chapters.append(html_to_markdown(archive.read(name).decode("utf-8", "replace")))
            except KeyError:
                continue
    return "\n\n".join(chapters)


# ---- email


def email_text(raw: bytes) -> tuple[str, str]:
    """(a one-line description, the readable text) of an .eml message: its
    subject, sender and date, then the plain-text body, or the HTML one
    converted. Attachments are named, not read."""
    message = BytesParser(policy=policy.default).parsebytes(raw)
    subject = str(message.get("subject", "") or "(no subject)")
    sender = str(message.get("from", "") or "unknown sender")
    date = str(message.get("date", "") or "")
    body_part = message.get_body(preferencelist=("plain", "html"))  # type: ignore[attr-defined]
    body = ""
    if body_part is not None:
        content = body_part.get_content()
        body = html_to_markdown(content) if body_part.get_content_type() == "text/html" else content
    attachments: list[str] = [
        name
        for part in message.iter_attachments()  # type: ignore[attr-defined]
        if (name := part.get_filename())
    ]
    header = f"Subject: {subject}\nFrom: {sender}\nDate: {date}"
    if attachments:
        header += f"\nAttachments: {', '.join(attachments)}"
    return f"email '{subject}' from {sender}" + (f", {date}" if date else ""), f"{header}\n\n{body}"


def chunk_email(
    raw: bytes, rel: str, *, capability: str | None = None, limits: Limits = DEFAULT_LIMITS
) -> Iterator[Chunk]:
    about, text = email_text(raw)
    for chunk in chunk_text(text, rel, capability=capability, limits=limits):
        yield replace(chunk, context=f"{about}, in {rel}")
