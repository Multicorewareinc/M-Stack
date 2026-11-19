"""Each input type the chunker reads, structured and unstructured, from a
small file built here; and the ones it refuses, with their reasons."""

from __future__ import annotations

import json
import zipfile
from email.message import EmailMessage
from itertools import pairwise
from pathlib import Path

import pytest
from chunking import (
    REDACTED,
    Limits,
    chunk_code,
    chunk_document,
    chunk_html,
    chunk_json,
    chunk_xml,
    chunk_yaml,
    document_files,
    is_secret_key,
    redact,
    redact_settings,
    skipped_files,
    unsupported_reason,
)

SMALL = Limits(max_tokens=60, overlap_tokens=15)


def write(tmp_path: Path, name: str, content: str | bytes) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content)
    return path


def contexts(chunks) -> list[str]:
    return [c.context for c in chunks]


# ---- code in other languages

GO = """package main

import "fmt"

// Server answers health checks.
type Server struct {
	Port int
}

// Start listens on the port.
// It never returns while the server runs.
func (s *Server) Start() error {
	addr := fmt.Sprintf(":%d", s.Port)

	return listen(addr)
}

func main() {
	s := Server{Port: 8080}
	s.Start()
}
"""


def test_a_braced_language_is_cut_at_its_top_level_declarations():
    chunks = list(chunk_code(GO, "main.go"))
    start = next(c for c in chunks if "func (s *Server) Start()" in c.context)
    assert start.text.startswith("// Start listens on the port.")
    assert "return listen(addr)" in start.text, "a blank line inside a function doesn't end it"
    assert any("type Server struct" in c.text and "Server answers" in c.text for c in chunks)


def test_small_declarations_are_joined():
    chunks = list(chunk_code(GO, "main.go"))
    header = chunks[0]
    assert "package main" in header.text and 'import "fmt"' in header.text
    assert header.context == "code in main.go, lines 1-3"


def test_a_brace_inside_a_string_or_comment_does_not_open_a_block():
    js = 'const open = "{";\n// a } in a comment\n\nfunction f() {\n  return 1;\n}\n'
    chunks = list(chunk_code(js, "a.js"))
    assert any(c.text.startswith("function f()") for c in chunks) or any(
        "function f()" in c.text for c in chunks
    )
    assert all(c.end_line <= 6 for c in chunks)


def test_sql_statements_separated_by_blank_lines_are_units():
    sql = (
        "CREATE TABLE plans (\n  id serial primary key,\n  name text not null,\n"
        "  tpm integer not null,\n  rpm integer not null\n);\n\n"
        "INSERT INTO plans (name, tpm, rpm) VALUES ('Free', 1000, 10), ('Pro', 100000, 600);\n"
    )
    chunks = list(chunk_code(sql, "seed.sql", limits=Limits(max_tokens=512, overlap_tokens=64)))
    assert any("CREATE TABLE plans" in c.text and ");" in c.text for c in chunks)


def test_a_long_function_in_another_language_is_split_with_overlap():
    body = "\n".join(f"  x{i} := compute({i})" for i in range(60))
    go = f"func long() {{\n{body}\n}}\n"
    parts = list(chunk_code(go, "l.go", limits=SMALL))
    assert len(parts) > 2 and parts[0].context.startswith("`func long() {` in l.go (part 1")
    assert all(b.start_line <= a.end_line for a, b in pairwise(parts))


def test_a_dockerfile_is_read_by_its_name(tmp_path):
    path = write(tmp_path, "Dockerfile", "FROM python:3.12-slim\n\nRUN pip install fastapi\n")
    assert [c.kind for c in chunk_document(path, "Dockerfile")] == ["code"]


# ---- JSON

CONFIG = {
    "service": {"name": "billing", "replicas": 2, "api_key": "sk-live-123", "token_ttl": 60},
    "plans": [{"name": f"tier-{i}", "tpm": 1000 * i, "notes": "x" * 80} for i in range(12)],
}


def test_json_that_fits_is_one_chunk_at_the_root():
    (chunk,) = chunk_json(json.dumps({"a": 1}), "c.json")
    assert chunk.context == "$ of c.json"


def test_large_json_is_split_by_key_path_and_small_siblings_are_joined():
    chunks = list(chunk_json(json.dumps(CONFIG), "c.json", limits=Limits(200, 20)))
    labels = contexts(chunks)
    assert "$.service of c.json" in labels or any("keys service" in c for c in labels)
    assert any(label.startswith("items ") and "of $.plans" in label for label in labels)
    assert all(json.loads(c.text) is not None for c in chunks if "(part" not in c.context)


def test_a_json_value_under_a_secret_key_is_redacted_whatever_it_looks_like():
    text = "\n".join(c.text for c in chunk_json(json.dumps(CONFIG), "c.json"))
    assert "sk-live-123" not in text and f'"api_key": "{REDACTED}"' in text
    assert '"token_ttl": 60' in text, "a TTL is not a token"


def test_json_lines_are_records_numbered_by_line(tmp_path):
    lines = "\n".join(json.dumps({"event": "request", "n": i}) for i in range(3))
    path = write(tmp_path, "events.jsonl", lines + "\n")
    (chunk,) = chunk_document(path, "events.jsonl")
    assert (chunk.start_line, chunk.end_line) == (1, 3)
    assert chunk.context == "records on lines 1-3 of events.jsonl"


# ---- YAML

VALUES = """# Settings for the gateway chart.
replicaCount: 2

image:
  # Pinned by digest so a re-tag can't change what runs.
  repository: registry/gateway
  tag: "1.4.0"

resources:
  limits:
    cpu: 500m
    memory: 512Mi
  requests:
    cpu: 100m
    memory: 128Mi

auth:
  password: hunter2
  existingSecretName: gateway-auth
"""


def test_yaml_small_entries_are_joined_with_their_comments():
    (chunk,) = chunk_yaml(VALUES, "values.yaml")
    assert "Pinned by digest" in chunk.text and chunk.start_line == 1


def test_a_long_yaml_key_is_split_into_the_keys_under_it_with_comments_and_lines():
    chunks = list(chunk_yaml(VALUES, "values.yaml", limits=Limits(30, 0)))
    image = next(c for c in chunks if "repository" in c.text)
    assert "Pinned by digest" in image.text
    lines = VALUES.splitlines()
    assert all(c.text.splitlines()[0] == lines[c.start_line - 1] for c in chunks)
    assert any("resources" in c.context for c in chunks)


def test_a_split_yaml_key_keeps_its_own_line_with_the_first_entry_under_it():
    chunks = list(chunk_yaml(VALUES, "values.yaml", limits=Limits(20, 0)))
    limits = next(c for c in chunks if "cpu: 500m" in c.text)
    assert "resources:" in limits.text or "limits:" in limits.text
    assert "resources.limits" in limits.context or "under resources" in limits.context


def test_yaml_list_items_are_numbered(tmp_path):
    text = "containers:\n" + "".join(
        f"  - name: c{i}\n    image: registry/c{i}:1.0\n    args: [{'a, ' * 15}b]\n"
        for i in range(3)
    )
    labels = contexts(chunk_yaml(text, "pod.yaml", limits=Limits(40, 0)))
    assert any("containers[0]" in label for label in labels)
    assert any("containers[2]" in label for label in labels)


def test_a_multi_document_yaml_names_each_document():
    text = "kind: Service\nname: a\n---\nkind: Deployment\nname: b\n"
    labels = contexts(chunk_yaml(text, "all.yaml"))
    assert labels[0].endswith("of document 1 all.yaml")
    assert labels[1].endswith("of document 2 all.yaml")


def test_a_yaml_password_is_redacted_but_a_secret_name_is_kept(tmp_path):
    (chunk,) = chunk_document(write(tmp_path, "values.yaml", VALUES), "values.yaml")
    assert "hunter2" not in chunk.text and f"password: {REDACTED}" in chunk.text
    assert "existingSecretName: gateway-auth" in chunk.text


def test_a_helm_template_reads_like_any_yaml(tmp_path):
    template = (
        "apiVersion: v1\nkind: Secret\ndata:\n  password: {{ .Values.auth.password | b64enc }}\n"
    )
    (chunk,) = chunk_document(write(tmp_path, "deployment.yaml", template), "deployment.yaml")
    assert "{{ .Values.auth.password | b64enc }}" in chunk.text


# ---- TOML and INI


def test_toml_and_ini_are_one_chunk_per_section(tmp_path):
    toml = '[tool.ruff]\nline-length = 100\n\n# Lints the tests too.\n[tool.pytest]\ntestpaths = ["tests"]\n'
    chunks = chunk_document(write(tmp_path, "pyproject.toml", toml), "pyproject.toml")
    assert contexts(chunks) == [
        "section [tool.ruff] of pyproject.toml",
        "section [tool.pytest] of pyproject.toml",
    ]
    assert chunks[1].text.startswith("# Lints the tests too.")


def test_settings_before_the_first_section_are_the_top(tmp_path):
    ini = "debug = false\n\n[database]\nhost = db\npassword = s3cr3t\n"
    chunks = chunk_document(write(tmp_path, "app.ini", ini), "app.ini")
    assert contexts(chunks)[0] == "section [top] of app.ini"
    assert "s3cr3t" not in chunks[1].text


# ---- tables

CSV = "region,month,revenue,api_key\nnorth,2026-01,1200,ak_1\nsouth,2026-01,900,ak_2\n"


def test_csv_rows_are_written_as_column_value_pairs(tmp_path):
    (chunk,) = chunk_document(write(tmp_path, "sales.csv", CSV), "sales.csv")
    assert chunk.text.splitlines()[0].startswith("region: north; month: 2026-01; revenue: 1200")
    assert chunk.context == "rows 2-3 of sales.csv (columns: region, month, revenue, api_key)"


def test_a_csv_column_named_like_a_secret_is_redacted(tmp_path):
    (chunk,) = chunk_document(write(tmp_path, "sales.csv", CSV), "sales.csv")
    assert "ak_1" not in chunk.text and f"api_key: {REDACTED}" in chunk.text


def test_the_delimiter_is_detected(tmp_path):
    text = "name;replicas\ngateway;2\nbilling;1\n"
    (chunk,) = chunk_document(write(tmp_path, "s.csv", text), "s.csv")
    assert "name: gateway; replicas: 2" in chunk.text


def test_many_rows_are_joined_up_to_the_limit(tmp_path):
    rows = "id,value\n" + "".join(f"{i},{'v' * 30}\n" for i in range(40))
    path = write(tmp_path, "big.tsv", rows.replace(",", "\t"))
    chunks = chunk_document(path, "big.tsv", limits=Limits(100, 0))
    assert len(chunks) > 1 and chunks[0].start_line == 2 and chunks[-1].end_line == 41


def _zip(files: dict[str, str]) -> bytes:
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'


def _xlsx() -> bytes:
    return _zip(
        {
            "xl/workbook.xml": f'<workbook {S} {R}><sheets><sheet name="Plans" sheetId="1" r:id="rId1"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
            "xl/sharedStrings.xml": f"<sst {S}><si><t>plan</t></si><si><t>tpm</t></si><si><t>Free</t></si></sst>",
            "xl/worksheets/sheet1.xml": f"<worksheet {S}><sheetData>"
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
            '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>1000</v></c></row>'
            '<row r="3"><c r="A3" t="inlineStr"><is><t>Pro</t></is></c><c r="C3"><v>9</v></c></row>'
            "</sheetData></worksheet>",
        }
    )


def test_an_excel_sheet_is_read_as_a_table_named_by_its_sheet(tmp_path):
    (chunk,) = chunk_document(write(tmp_path, "plans.xlsx", _xlsx()), "plans.xlsx")
    assert chunk.text.splitlines() == ["plan: Free; tpm: 1000", "plan: Pro; column 3: 9"]
    assert chunk.context.startswith("rows 2-3 of sheet 'Plans' in plans.xlsx")


# ---- XML and HTML


def test_xml_is_split_by_element_path():
    servers = "".join(
        f"<server><name>s{i}</name><port>{8000 + i}</port><note>{'n' * 60}</note></server>"
        for i in range(6)
    )
    chunks = list(
        chunk_xml(
            f"<config><servers>{servers}</servers><debug>false</debug></config>",
            "app.xml",
            limits=Limits(80, 0),
        )
    )
    labels = contexts(chunks)
    assert labels[0] == "elements server[1], server[2] under /config/servers of app.xml"
    assert "element /config/debug of app.xml" in labels


HTML = """<html><head><title>Gateway guide</title><style>p { color: red }</style></head>
<body><nav><a href="/">Home</a></nav>
<h2>Install</h2><p>Run the <b>chart</b>.</p><pre>helm install gw ./chart</pre>
<h2>Limits</h2><ul><li>60 requests a minute</li><li>Burst of 10</li></ul>
<table><tr><th>Plan</th><th>RPM</th></tr><tr><td>Free</td><td>60</td></tr></table>
<script>track()</script></body></html>"""


def test_html_is_cut_by_its_headings_without_scripts_or_styles():
    chunks = list(chunk_html(HTML, "guide.html"))
    labels = contexts(chunks)
    assert "section 'Gateway guide > Install' of guide.html" in labels
    limits = next(c for c in chunks if "Limits" in c.context)
    assert "- 60 requests a minute" in limits.text and "Free | 60" in limits.text
    text = "\n".join(c.text for c in chunks)
    assert "track()" not in text and "color: red" not in text
    assert "```\nhelm install gw ./chart\n```" in text


# ---- office documents, e-books, email, notebooks

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _docx() -> bytes:
    def p(text: str, style: str = "", listed: bool = False) -> str:
        props = ""
        if style or listed:
            props = "<w:pPr>" + (f'<w:pStyle w:val="{style}"/>' if style else "")
            props += "<w:numPr/>" if listed else ""
            props += "</w:pPr>"
        return f"<w:p>{props}<w:r><w:t>{text}</w:t></w:r></w:p>"

    body = (
        p("Runbook", "Title")
        + p("Restarting", "Heading1")
        + p("Scale the deployment to zero, then back.")
        + p("Check the logs", listed=True)
        + "<w:tbl><w:tr><w:tc>"
        + p("Step")
        + "</w:tc><w:tc>"
        + p("Owner")
        + "</w:tc></w:tr></w:tbl>"
    )
    return _zip({"word/document.xml": f"<w:document {W}><w:body>{body}</w:body></w:document>"})


def test_a_word_document_keeps_its_headings_lists_and_tables(tmp_path):
    chunks = chunk_document(write(tmp_path, "runbook.docx", _docx()), "runbook.docx")
    restart = next(c for c in chunks if "Restarting" in c.context)
    assert restart.context == "section 'Runbook > Restarting' of runbook.docx"
    assert "Scale the deployment to zero" in restart.text
    assert "- Check the logs" in restart.text and "Step | Owner" in restart.text


def _odt() -> bytes:
    ns = 'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
    return _zip(
        {
            "content.xml": f"<office:document-content {ns}><office:body><office:text>"
            '<text:h text:outline-level="1">Backups</text:h><text:p>Nightly at 02:00.</text:p>'
            "</office:text></office:body></office:document-content>"
        }
    )


def test_an_opendocument_text_keeps_its_headings(tmp_path):
    (chunk,) = chunk_document(write(tmp_path, "ops.odt", _odt()), "ops.odt")
    assert chunk.context == "section 'Backups' of ops.odt" and "Nightly at 02:00." in chunk.text


def _pptx() -> bytes:
    a = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'

    def slide(*lines: str) -> str:
        return (
            f"<p:sld {a} xmlns:p='p'>"
            + "".join(f"<a:p><a:r><a:t>{t}</a:t></a:r></a:p>" for t in lines)
            + "</p:sld>"
        )

    return _zip(
        {
            "ppt/slides/slide1.xml": slide("Architecture", "Gateway in front"),
            "ppt/slides/slide2.xml": slide("Billing", "Stripe meters usage"),
            "ppt/slides/slide10.xml": slide("Questions"),
            "ppt/notesSlides/notesSlide2.xml": slide("Mention the outbox."),
        }
    )


def test_a_deck_is_read_slide_by_slide_with_notes_in_order(tmp_path):
    (chunk,) = chunk_document(write(tmp_path, "deck.pptx", _pptx()), "deck.pptx")
    assert chunk.context == "slides 1-3 of deck.pptx"
    text = chunk.text
    assert text.index("Architecture") < text.index("Billing") < text.index("Questions")
    assert "Notes: Mention the outbox." in text


def _epub() -> bytes:
    return _zip(
        {
            "META-INF/container.xml": '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/book.opf"/></rootfiles></container>',
            "OEBPS/book.opf": '<package xmlns="http://www.idpf.org/2007/opf"><manifest>'
            '<item id="c1" href="one.xhtml"/><item id="c2" href="two.xhtml"/></manifest>'
            '<spine><itemref idref="c2"/><itemref idref="c1"/></spine></package>',
            "OEBPS/one.xhtml": "<html><body><h1>Later</h1><p>Second in reading order.</p></body></html>",
            "OEBPS/two.xhtml": "<html><body><h1>First</h1><p>Read first.</p></body></html>",
        }
    )


def test_an_epub_is_read_in_its_reading_order(tmp_path):
    chunks = chunk_document(write(tmp_path, "book.epub", _epub()), "book.epub")
    assert contexts(chunks) == ["section 'First' of book.epub", "section 'Later' of book.epub"]


def test_an_email_is_described_by_subject_sender_and_date(tmp_path):
    message = EmailMessage()
    message["Subject"] = "Gateway outage"
    message["From"] = "ops@example.com"
    message["Date"] = "Wed, 30 Sep 2026 10:00:00 +0000"
    message.set_content("The gateway was down for 5 minutes.\n\nRoot cause: an expired cert.")
    message.add_attachment(b"%PDF", maintype="application", subtype="pdf", filename="report.pdf")
    (chunk,) = chunk_document(write(tmp_path, "outage.eml", message.as_bytes()), "outage.eml")
    assert chunk.context.startswith("email 'Gateway outage' from ops@example.com")
    assert "down for 5 minutes" in chunk.text and "Attachments: report.pdf" in chunk.text


def test_a_notebook_keeps_its_prose_and_code_but_not_its_outputs(tmp_path):
    notebook = {
        "metadata": {"kernelspec": {"language": "python"}},
        "cells": [
            {"cell_type": "markdown", "source": ["# Load test\n", "Ramp to 100 rps."]},
            {"cell_type": "code", "source": "run(rps=100)", "outputs": [{"text": "OUTPUT-NOISE"}]},
        ],
    }
    (chunk,) = chunk_document(write(tmp_path, "load.ipynb", json.dumps(notebook)), "load.ipynb")
    assert "Ramp to 100 rps." in chunk.text and "```python\nrun(rps=100)\n```" in chunk.text
    assert "OUTPUT-NOISE" not in chunk.text


# ---- what is not read, and why


@pytest.mark.parametrize(
    "name, reason",
    [
        ("secret.yaml", "credential"),
        ("diagram.png", "OCR"),
        ("call.mp3", "speech-to-text"),
        ("old.doc", "legacy Office"),
        ("bundle.zip", "unpack"),
        ("app.exe", "binary"),
        (".env", "credential"),
        ("server.key", "credential"),
        ("credentials.json", "credential"),
        ("poetry.lock", "lock file"),
    ],
)
def test_what_is_not_read_says_why(tmp_path, name, reason):
    assert reason in unsupported_reason(write(tmp_path, name, "x"))


def test_source_code_about_credentials_is_still_read(tmp_path):
    assert unsupported_reason(write(tmp_path, "credentials.py", "X = 1\n")) is None


def test_a_file_of_unknown_type_is_read_when_it_is_text(tmp_path):
    assert unsupported_reason(write(tmp_path, "NOTES", "plain words\n")) is None
    assert "binary" in unsupported_reason(write(tmp_path, "blob", b"\x00\x01\x02"))


def test_a_folder_scan_reports_what_it_skipped(tmp_path):
    write(tmp_path, "a.md", "# A\n\nText.\n")
    write(tmp_path, "pic.png", b"\x89PNG")
    write(tmp_path, "b.docx", _docx())
    assert [f.name for f in document_files(tmp_path)] == ["a.md", "b.docx"]
    assert {r: [f.name for f in fs] for r, fs in skipped_files(tmp_path).items()} == {
        "an image; reading its text needs OCR": ["pic.png"]
    }


def test_a_damaged_office_file_is_refused_not_crashed_on(tmp_path):
    with pytest.raises(ValueError):
        chunk_document(write(tmp_path, "broken.docx", b"not a zip"), "broken.docx")


def test_an_archive_that_unpacks_too_large_is_refused(tmp_path, monkeypatch):
    import chunking.documents as documents

    monkeypatch.setattr(documents, "MAX_UNPACKED_BYTES", 10)
    with pytest.raises(ValueError, match="unpacks"):
        chunk_document(write(tmp_path, "big.docx", _docx()), "big.docx")


# ---- credentials in structured text


@pytest.mark.parametrize(
    "key, secret",
    [
        ("SERVICE_API_KEY", True),
        ("clientSecret", True),
        ("db.password", True),
        ("accessKey", True),
        ("max_tokens", False),
        ("tokenizer_url", False),
        ("NODE_TOKEN_PATH", False),
        ("existing_secret_name", False),
        ("secretKeyRef", False),
    ],
)
def test_which_names_hold_a_credential(key, secret):
    assert is_secret_key(key) is secret


def test_placeholders_paths_and_references_are_not_redacted():
    text = (
        'NODE_TOKEN_PATH = "/var/lib/rancher/rke2/server/node-token"\n'
        "api_key_secret=...\n"
        "password: {{ .Values.password }}\n"
        "token: ${TOKEN}\n"
        "api_key: <your-key>\n"
    )
    assert redact_settings(text) == text
    assert redact(text) == text
