"""Parser + spotlighting tests. PDF/docx fixtures are generated in-process so
there are no binary blobs in the repo."""
import io

import pymupdf
from docx import Document

from pipeline.parsers import SPACE_MARK, parse_docx, parse_pdf, parse_whatsapp, spotlight


def _make_pdf(body: str, *, metadata: dict | None = None) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), body)
    if metadata:
        doc.set_metadata(metadata)
    data = doc.tobytes()
    doc.close()
    return data


def _make_docx(paragraphs: list[str]) -> bytes:
    d = Document()
    for p in paragraphs:
        d.add_paragraph(p)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_parse_pdf_extracts_text():
    data = _make_pdf("Deadline is Friday")
    text = parse_pdf(data)
    assert "Deadline is Friday" in text


def test_parse_pdf_does_not_leak_metadata():
    data = _make_pdf("Visible body text", metadata={"title": "SECRET-INJECT-PAYLOAD"})
    text = parse_pdf(data)
    assert "Visible body text" in text
    assert "SECRET-INJECT-PAYLOAD" not in text  # metadata not passed through


def test_parse_docx_extracts_paragraphs():
    data = _make_docx(["Project brief", "Ship the report by Friday"])
    text = parse_docx(data)
    assert "Project brief" in text
    assert "Ship the report by Friday" in text


def test_parse_whatsapp_normalises_and_drops_system_lines():
    export = (
        "[31/12/2023, 23:59:59] Alice: Happy new year\n"
        "12/31/23, 11:59 PM - Bob: See you tomorrow\n"
        "01/01/24, 00:00 - Messages and calls are end-to-end encrypted.\n"
    )
    out = parse_whatsapp(export)
    lines = out.splitlines()
    assert lines == ["Alice: Happy new year", "Bob: See you tomorrow"]


def test_parse_whatsapp_joins_continuations():
    export = (
        "[01/01/24, 10:00:00] Alice: first line\n"
        "second line continues\n"
    )
    assert parse_whatsapp(export) == "Alice: first line second line continues"


def test_spotlight_datamarks_spaces():
    marked = spotlight("add this to memory")
    assert " " not in marked
    assert SPACE_MARK in marked
    assert marked.replace(SPACE_MARK, " ") == "add this to memory"  # reversible
