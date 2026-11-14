"""server/convert.py: Office files to Markdown (docs/tasks/share-and-files.md,
Phase 1, "Conversion"). Fixtures are built here with the converter's own
libraries, and the content is invented."""
from __future__ import annotations

import io
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from server import convert
from tests.office_fixtures import make_docx, make_pptx, make_xlsx, zip_bytes

REPO_ROOT = Path(__file__).resolve().parent.parent


def _file(tmp_path, data: bytes, name: str) -> str:
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def _convert(tmp_path, kind: str, data: bytes) -> str:
    return convert.convert(kind, _file(tmp_path, data, f"in.{kind}"))


# --- Word ----------------------------------------------------------------------


def test_docx_headings_keep_their_level_and_tables_become_markdown_tables(tmp_path):
    md = _convert(tmp_path, "docx", make_docx())
    assert "# Garden plan" in md.splitlines()
    assert "## Beds" in md.splitlines()
    assert "Tomatoes go in the south bed." in md
    assert "| Bed | Crop |" in md
    assert "| --- | --- |" in md
    # A pipe in a cell must not split it.
    assert "| South | Tomato \\| basil |" in md
    assert "- Water twice a week." in md


def test_docx_body_order_is_kept(tmp_path):
    md = _convert(tmp_path, "docx", make_docx())
    assert md.index("# Garden plan") < md.index("Tomatoes") < md.index("## Beds") < md.index("| Bed |")


# --- Excel ---------------------------------------------------------------------


def test_xlsx_each_sheet_is_a_heading_and_a_table_of_values(tmp_path):
    md = _convert(tmp_path, "xlsx", make_xlsx(rows=2, sheets=2))
    lines = md.splitlines()
    assert "## Budget" in lines
    assert "| Item | Cost |" in lines
    assert "| Thing 0 | 10 |" in lines
    assert "| Thing 1 | 11 |" in lines
    assert "## Extra 1" in lines
    assert "| only | row |" in lines


def test_xlsx_shows_cell_values_not_formulas(tmp_path):
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["a", "b"])
    ws.append([1, "=SUM(A2:A2)"])
    buf = io.BytesIO()
    wb.save(buf)
    md = _convert(tmp_path, "xlsx", buf.getvalue())
    # No cached value was ever computed for it, so there is nothing to show;
    # what must never appear is the formula text.
    assert "=SUM" not in md
    assert "| 1 |" in md


def test_xlsx_rows_per_sheet_are_capped_and_the_cut_is_said_up_top(tmp_path, monkeypatch):
    monkeypatch.setattr(convert, "MAX_ROWS_PER_SHEET", 5)
    md = _convert(tmp_path, "xlsx", make_xlsx(rows=20, sheets=1))
    assert "| Thing 3 |" in md
    assert "| Thing 5 |" not in md
    first = md.splitlines()[0]
    assert first.startswith("> ") and "cut" in first.lower() and "Budget" in first


def test_xlsx_sheets_are_capped_and_the_cut_is_said_up_top(tmp_path, monkeypatch):
    monkeypatch.setattr(convert, "MAX_SHEETS", 2)
    md = _convert(tmp_path, "xlsx", make_xlsx(rows=1, sheets=4))
    assert "## Extra 1" in md
    assert "## Extra 2" not in md
    assert md.startswith("> ") and "cut" in md.splitlines()[0].lower()


# --- PowerPoint ----------------------------------------------------------------


def test_pptx_slides_have_a_numbered_heading_a_body_and_notes(tmp_path):
    md = _convert(tmp_path, "pptx", make_pptx())
    lines = md.splitlines()
    assert "## Slide 1: Kickoff" in lines
    assert "Agree the dates" in lines
    assert "Notes:" in lines
    assert "Say hello first" in lines
    assert "## Slide 2: Next steps" in lines
    assert md.index("Slide 1") < md.index("Say hello first") < md.index("Slide 2")
    # Slide 2 has no notes, so only the first slide carries the label.
    assert md.count("Notes:") == 1


# --- the character cap ----------------------------------------------------------


def test_long_output_is_cut_at_a_paragraph_boundary_with_a_note_on_top(tmp_path, monkeypatch):
    import docx

    doc = docx.Document()
    for i in range(50):
        doc.add_paragraph(f"Paragraph number {i:02d} with some padding words.")
    buf = io.BytesIO()
    doc.save(buf)
    monkeypatch.setattr(convert, "MAX_CHARS", 400)

    md = _convert(tmp_path, "docx", buf.getvalue())

    first, _, body = md.partition("\n\n")
    assert first.startswith("> ") and "cut" in first.lower()
    assert len(body) <= 400
    # Whole paragraphs only: the last one is complete, the next is absent.
    last = body.rstrip().splitlines()[-1]
    assert last.endswith("padding words.")
    assert "Paragraph number 49" not in body
    # And the note says where.
    assert "paragraph" in first.lower()


def test_output_under_the_cap_has_no_note(tmp_path):
    assert not _convert(tmp_path, "docx", make_docx()).startswith("> ")


# --- refusals --------------------------------------------------------------------


def test_a_zip_bomb_is_refused_before_anything_is_parsed(tmp_path, monkeypatch):
    # Declared sizes are what the guard reads, so a tiny limit stands in for
    # 200MB of zeros without making the test pay for them.
    monkeypatch.setattr(convert, "MAX_UNCOMPRESSED_BYTES", 1000)
    data = zip_bytes({"[Content_Types].xml": "<Types/>", "word/document.xml": "x" * 5000})
    with pytest.raises(convert.ConvertError, match="archive"):
        _convert(tmp_path, "docx", data)


def test_too_many_entries_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(convert, "MAX_ZIP_ENTRIES", 3)
    parts = {"[Content_Types].xml": "<Types/>", "word/document.xml": "<x/>", "a": "", "b": ""}
    with pytest.raises(convert.ConvertError, match="archive"):
        _convert(tmp_path, "docx", zip_bytes(parts))


def test_the_real_limits_are_the_documented_ones():
    assert convert.MAX_UNCOMPRESSED_BYTES == 200 * 1024 * 1024
    assert convert.MAX_ZIP_ENTRIES == 5000
    assert convert.MAX_CHARS == 200_000
    assert convert.MAX_ROWS_PER_SHEET == 2000
    assert convert.MAX_SHEETS == 20


def _with_member(data: bytes, name: str, replacement: bytes) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            dst.writestr(info.filename, replacement if info.filename == name else src.read(info))
    return out.getvalue()


def _xxe_docx(canary: Path) -> bytes:
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<!DOCTYPE doc [<!ENTITY leak SYSTEM "file://{canary}">]>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>before &leak; after</w:t></w:r></w:p></w:body></w:document>"
    )
    return _with_member(make_docx(), "word/document.xml", xml.encode())


def test_a_document_type_declaration_is_refused(tmp_path):
    canary = tmp_path / "canary.txt"
    canary.write_text("CANARY-VALUE-1234")
    with pytest.raises(convert.ConvertError, match="DTD"):
        _convert(tmp_path, "docx", _xxe_docx(canary))


def test_the_parsers_do_not_expand_external_entities_even_without_the_scan(tmp_path, monkeypatch):
    # The second wall: if the DTD scan were ever bypassed, python-docx's own
    # parser (resolve_entities=False) must still not read the file.
    canary = tmp_path / "canary.txt"
    canary.write_text("CANARY-VALUE-1234")
    monkeypatch.setattr(convert, "_refuse_dtd", lambda zf: None)
    try:
        md = _convert(tmp_path, "docx", _xxe_docx(canary))
    except Exception:
        return  # refusing is as good as not expanding
    assert "CANARY-VALUE-1234" not in md


def test_a_dtd_hidden_in_utf16_is_refused_too(tmp_path):
    xml = '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY e "x">]><d/>'.encode("utf-16")
    data = _with_member(make_docx(), "word/document.xml", xml)
    with pytest.raises(convert.ConvertError, match="DTD"):
        _convert(tmp_path, "docx", data)


def test_a_document_with_no_text_is_a_failure_not_an_empty_file(tmp_path):
    import docx

    buf = io.BytesIO()
    docx.Document().save(buf)
    with pytest.raises(convert.ConvertError, match="no text"):
        _convert(tmp_path, "docx", buf.getvalue())


def test_something_that_is_not_a_zip_is_a_failure(tmp_path):
    with pytest.raises(convert.ConvertError):
        _convert(tmp_path, "pptx", b"not a zip at all")


# --- the command line ---------------------------------------------------------------


def _run(argv, **kwargs):
    return subprocess.run(
        [sys.executable, "-s", "-m", "server.convert", *argv],
        cwd=REPO_ROOT, capture_output=True, stdin=subprocess.DEVNULL, timeout=60, **kwargs,
    )


def test_the_command_line_prints_markdown_to_stdout_and_writes_nothing(tmp_path):
    path = _file(tmp_path, make_docx(), "in.docx")
    before = sorted(os.listdir(tmp_path))
    proc = _run(["docx", path])
    assert proc.returncode == 0, proc.stderr
    assert b"# Garden plan" in proc.stdout
    assert sorted(os.listdir(tmp_path)) == before


def test_the_command_line_fails_with_a_status_not_a_traceback_dump_of_content(tmp_path):
    path = _file(tmp_path, b"SECRET-BODY-TEXT", "in.docx")
    proc = _run(["docx", path])
    assert proc.returncode == 1
    assert proc.stdout == b""
    # The reason is a short class name or fixed phrase, never the document.
    assert b"SECRET-BODY-TEXT" not in proc.stderr


def test_the_command_line_rejects_an_unknown_kind_and_wrong_arity(tmp_path):
    path = _file(tmp_path, make_docx(), "in.docx")
    assert _run(["rtf", path]).returncode == 2
    assert _run(["docx"]).returncode == 2
