"""The generic chunker: each file type, splitting with overlap, redaction,
and which files a folder of documents contributes."""

from __future__ import annotations

from itertools import pairwise

import pytest
from chunking import (
    REDACTED,
    Limits,
    chunk_document,
    chunk_markdown,
    chunk_pdf_pages,
    chunk_python,
    chunk_text,
    document_files,
    estimate_tokens,
    redact,
    split_lines,
)

SMALL = Limits(max_tokens=60, overlap_tokens=15)


def by_context(chunks, needle: str):
    matches = [c for c in chunks if needle in c.context]
    assert len(matches) == 1, [c.context for c in chunks]
    return matches[0]


# ---- Python

NESTED = '''def outer(items):
    """Sorts, then keeps the first few."""

    def key(item):
        return item.priority

    def keep(item):
        """Only the urgent ones."""
        return item.priority > 5

    return [i for i in sorted(items, key=key) if keep(i)]


class Service:
    class Config:
        retries: int = 3

    def run(self):
        def step():
            return 1

        return step()
'''


def test_a_nested_function_gets_its_own_chunk_and_stays_in_its_parent():
    chunks = list(chunk_python(NESTED, "svc.py"))
    keep = by_context(chunks, "function 'keep' inside function 'outer'")
    assert keep.text.lstrip().startswith("def keep") and "urgent" in keep.text
    assert (keep.start_line, keep.end_line) == (7, 9)
    (outer,) = [c for c in chunks if c.context == "function 'outer', in svc.py"]
    assert "def keep" in outer.text


def test_a_function_nested_in_a_method_is_found():
    chunks = list(chunk_python(NESTED, "svc.py"))
    assert by_context(chunks, "function 'step' inside function 'run'")
    assert by_context(chunks, "method 'run' of class Service")


def test_a_class_nested_in_a_class_is_named_by_both():
    chunks = list(chunk_python(NESTED, "svc.py"))
    assert "class Service.Config, in svc.py" in [c.context for c in chunks]
    assert by_context(chunks, "field 'retries' of class Service.Config")


def test_a_definition_under_a_type_checking_guard_is_found():
    text = "import typing\n\nif typing.TYPE_CHECKING:\n    def hinted():\n        return 1\n"
    assert by_context(list(chunk_python(text, "g.py")), "function 'hinted'")


def _long_function(lines: int) -> str:
    body = "\n".join(f"    step_{i} = compute({i})" for i in range(lines))
    return f"def long():\n{body}\n    return step_0\n"


def test_a_function_too_long_for_one_chunk_is_split_into_overlapping_parts():
    parts = list(chunk_python(_long_function(60), "l.py", limits=SMALL))
    assert len(parts) > 2
    assert parts[0].context.endswith(f"(part 1 of {len(parts)})")
    assert all(estimate_tokens(p.text) <= SMALL.max_tokens for p in parts)
    for before, after in pairwise(parts):
        assert after.start_line <= before.end_line, "consecutive parts share lines"
        assert before.text.splitlines()[-1] in after.text


def test_split_parts_cover_every_line_and_have_unique_ids():
    parts = list(chunk_python(_long_function(60), "l.py", limits=SMALL))
    covered = {n for p in parts for n in range(p.start_line, p.end_line + 1)}
    assert covered == set(range(1, 63))
    assert len({p.id for p in parts}) == len(parts)


def test_a_unit_that_fits_is_never_split():
    assert len(split_lines(["a = 1", "b = 2"], 1, SMALL)) == 1


def test_a_single_line_longer_than_a_chunk_is_cut_to_fit():
    windows = split_lines(["x" * 1000, "y = 1"], 1, SMALL)
    assert all(len(text) <= SMALL.max_tokens * 4 for _, _, text in windows)


# ---- Markdown

README = """# Billing

Meters token usage and reports it to Stripe.

## Configuration

### Environment

Set `STRIPE_API_KEY` from a Secret.

```bash
# a heading-looking comment inside a fence
helm install billing ./chart
```

## Running locally

Start the service.
"""


def test_markdown_is_one_chunk_per_section_labelled_by_its_headings():
    chunks = list(chunk_markdown(README, "billing/README.md"))
    assert [c.context for c in chunks] == [
        "section 'Billing' of billing/README.md",
        "section 'Billing > Configuration > Environment' of billing/README.md",
        "section 'Billing > Running locally' of billing/README.md",
    ]


def test_a_heading_with_only_subheadings_under_it_gets_no_chunk_of_its_own():
    contexts = [c.context for c in chunk_markdown(README, "r.md")]
    assert "section 'Billing > Configuration' of r.md" not in contexts


def test_a_hash_inside_a_code_fence_is_not_a_heading():
    env = by_context(list(chunk_markdown(README, "r.md")), "Environment")
    assert "# a heading-looking comment inside a fence" in env.text
    assert "helm install billing" in env.text


def test_a_section_records_its_own_lines():
    env = by_context(list(chunk_markdown(README, "r.md")), "Environment")
    lines = README.splitlines()
    assert lines[env.start_line - 1] == "### Environment"
    assert lines[env.end_line - 1] == "```"


def test_text_before_the_first_heading_is_the_introduction():
    chunks = list(chunk_markdown("Some intro.\n\n# Title\n\nBody.\n", "r.md"))
    assert chunks[0].context == "section 'introduction' of r.md"


def test_a_long_section_is_split_between_paragraphs_with_overlap_and_keeps_its_heading():
    paragraphs = "\n\n".join(f"Paragraph {i} " + "words " * 20 for i in range(8))
    parts = list(chunk_markdown(f"## Deploying\n\n{paragraphs}\n", "r.md", limits=SMALL))
    assert len(parts) > 2
    assert all(p.text.startswith("## Deploying") for p in parts)
    for before, after in pairwise(parts):
        assert before.text[-30:] in after.text, "each part repeats the end of the one before"


def test_a_code_fence_that_fits_is_never_split():
    fence = "```python\n" + "\n".join(f"x{i} = {i}" for i in range(12)) + "\n```"
    text = "## Example\n\n" + "intro " * 30 + "\n\n" + fence + "\n\n" + "outro " * 30 + "\n"
    parts = list(chunk_markdown(text, "r.md", limits=Limits(max_tokens=80, overlap_tokens=10)))
    assert any(fence in p.text for p in parts)


# ---- plain text and PDF


def test_plain_text_is_cut_on_paragraphs_with_overlap():
    text = "\n\n".join(f"Paragraph {i}. " + "words " * 25 for i in range(6))
    parts = list(chunk_text(text, "notes.txt", limits=SMALL))
    assert len(parts) > 2
    assert all(p.kind == "text" for p in parts)
    for before, after in pairwise(parts):
        assert before.text[-30:] in after.text
        assert after.start_line <= before.end_line


def test_pdf_chunks_are_numbered_by_page():
    pages = ["First page text.", "", "Third page, after a scanned one."]
    chunks = list(chunk_pdf_pages(pages, "manual.pdf"))
    assert len(chunks) == 1
    assert (chunks[0].start_line, chunks[0].end_line) == (1, 3)
    assert chunks[0].context == "pages 1-3 of manual.pdf"


def _pdf(pages: list[str]) -> bytes:
    """A minimal PDF with one line of Helvetica text per page."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages)))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>")
    font = 3 + 2 * len(pages)
    for i, text in enumerate(pages):
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * i} 0 R "
            f"/Resources << /Font << /F1 {font} 0 R >> >> >>"
        )
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out, offsets = b"%PDF-1.4\n", []
    for n, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out


def test_a_pdf_file_is_read_page_by_page(tmp_path):
    pytest.importorskip("pypdf")
    path = tmp_path / "guide.pdf"
    path.write_bytes(_pdf(["Install the chart first.", "Then set the replicas."]))
    (chunk,) = chunk_document(path, "docs/guide.pdf")
    assert "Install the chart first." in chunk.text and "Then set the replicas." in chunk.text
    assert (chunk.start_line, chunk.end_line) == (1, 2)


# ---- by file type


def test_each_supported_type_goes_to_its_chunker(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    (tmp_path / "b.md").write_text("# B\n\nBody.\n")
    (tmp_path / "c.txt").write_text("Plain words.\n")
    kinds = {
        name: [c.kind for c in chunk_document(tmp_path / name, name)]
        for name in ("a.py", "b.md", "c.txt")
    }
    assert kinds == {"a.py": ["function"], "b.md": ["section"], "c.txt": ["text"]}


def test_an_unsupported_type_is_refused(tmp_path):
    (tmp_path / "x.bin").write_bytes(b"\0")
    with pytest.raises(ValueError):
        chunk_document(tmp_path / "x.bin", "x.bin")


def test_a_folder_contributes_only_readable_documents(tmp_path):
    for rel in [
        "guide.md",
        "notes/setup.txt",
        "manual.pdf",
        "code/app.py",
        ".env",
        ".env.production",
        "certs/server.key",
        "secrets.yaml",
        "image.png",
        "node_modules/lib/index.md",
        ".git/HEAD.txt",
        ".hidden/notes.md",
        "venv/lib/site.py",
    ]:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    found = [f.relative_to(tmp_path).as_posix() for f in document_files(tmp_path)]
    assert found == ["code/app.py", "guide.md", "manual.pdf", "notes/setup.txt"]


# ---- credentials


@pytest.mark.parametrize(
    "secret",
    [
        "gsk_" + "a1B2" * 8,
        "sk-" + "Ab3" * 10,
        "ghp_" + "Z9y8" * 9,
        "AKIA" + "ABCD1234EFGH5678",
    ],
)
def test_provider_keys_are_redacted(secret):
    assert redact(f"key is {secret} here") == f"key is {REDACTED} here"


def test_a_generated_value_assigned_to_a_secret_name_is_redacted():
    assert redact("SERVICE_API_KEY=lW5gBDoHZifqc6Jwv7Fb") == f"SERVICE_API_KEY={REDACTED}"
    assert redact('  db_password: "Xk29fjQ81mZpa0Lw"') == f'  db_password: "{REDACTED}"'


def test_code_that_only_names_a_secret_is_left_alone():
    code = "password = settings.db_password\napi_key: str = Field(default=None)"
    assert redact(code) == code


def test_a_private_key_block_is_redacted():
    block = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"
    assert redact(f"before\n{block}\nafter") == f"before\n{REDACTED}\nafter"


def test_documents_come_back_redacted(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text("# Notes\n\nOur key: gsk_" + "Q7w" * 10 + "\n")
    (chunk,) = chunk_document(path, "notes.md")
    assert "gsk_" not in chunk.text and REDACTED in chunk.text
