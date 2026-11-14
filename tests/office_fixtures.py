"""Tiny invented Office files, built with the converter's own libraries so no
binary is committed (docs/tasks/share-and-files.md, Phase 1 tests). Content is
made up on purpose: nothing here is from a real box or account."""
from __future__ import annotations

import io
import zipfile


def zip_bytes(parts: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in parts.items():
            zf.writestr(name, data)
    return buf.getvalue()


def fake_ooxml(main_part: str, *, content_types: bool = True) -> bytes:
    """A zip that has the structure sniffing looks at and nothing else. Never
    meant to be opened by a parser."""
    parts: dict[str, bytes | str] = {main_part: "<x/>"}
    if content_types:
        parts["[Content_Types].xml"] = "<Types/>"
    return zip_bytes(parts)


def make_docx() -> bytes:
    import docx

    doc = docx.Document()
    doc.add_heading("Garden plan", level=1)
    doc.add_paragraph("Tomatoes go in the south bed.")
    doc.add_heading("Beds", level=2)
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Bed"
    table.cell(0, 1).text = "Crop"
    table.cell(1, 0).text = "South"
    table.cell(1, 1).text = "Tomato | basil"
    doc.add_paragraph("Water twice a week.", style="List Bullet")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def make_xlsx(rows: int = 3, sheets: int = 2) -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    first = wb.active
    first.title = "Budget"
    first.append(["Item", "Cost"])
    for i in range(rows):
        first.append([f"Thing {i}", 10 + i])
    for n in range(1, sheets):
        wb.create_sheet(f"Extra {n}").append(["only", "row"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def make_pptx() -> bytes:
    import pptx

    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "Kickoff"
    slide.placeholders[1].text = "Agree the dates"
    slide.notes_slide.notes_text_frame.text = "Say hello first"
    second = prs.slides.add_slide(prs.slide_layouts[1])
    second.shapes.title.text = "Next steps"
    second.placeholders[1].text = "Book the room"
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()
