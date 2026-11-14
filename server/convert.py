"""Office files to Markdown (docs/tasks/share-and-files.md, Phase 1).

Run as `python -m server.convert <docx|xlsx|pptx> <input>`: the Markdown goes
to STDOUT and nothing is written, so the process needs no writable mount.
server/attachments.py runs it under bubblewrap with no network and nothing
bound but this checkout, the interpreter and the one input file
(`_converter_argv`). These parsers read a stranger's document, forwarded;
a parser bug must happen where there is nothing worth having.

The third-party libraries (python-docx, openpyxl, python-pptx) are imported
inside the converters, not here, the way server/transcribe.py imports
`sherpa_onnx`: the server imports this module for the archive limits, and
startup should not pay for three parsers.

Exit status is the contract: 0 with Markdown on stdout, anything else is a
failed conversion (1 for a file it could not read, 2 for a bad command line).
Failure reasons on stderr are fixed phrases or an exception's class name,
never text from the document.
"""
from __future__ import annotations

import re
import sys
import zipfile
from collections.abc import Callable
from typing import Any

# Zip-bomb guard, applied by the server before it spawns this (on the central
# directory alone) and again here, because this file is also the thing that
# would be fed a bomb if the first check were ever skipped.
MAX_ZIP_ENTRIES = 5000
MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024

# About 200,000 characters of Markdown per file, cut at a paragraph or row
# boundary (the spec's limit; Read takes the rest in ranges, but a text
# version this long is already more than a turn should be asked to carry).
MAX_CHARS = 200_000
MAX_ROWS_PER_SHEET = 2000
MAX_SHEETS = 20

# What this process may use, set on itself in `main` rather than by the
# server's `preexec_fn`: the server is multi-threaded, and Python warns that
# preexec_fn is unsafe there. A limit raised again by an exploited parser
# would need the hard limit, which is lowered too.
_MEMORY_LIMIT_BYTES = 512 * 1024 * 1024
# stdout and stderr are files the server opened, so this is also the most the
# process can ever hand back (the server reads at most 2MB of it).
_OUTPUT_LIMIT_BYTES = 4 * 1024 * 1024


class ConvertError(Exception):
    """A file this converter will not or cannot read. The message is a fixed
    phrase: it may reach a log, so it never quotes the document."""


def archive_problem(zf: zipfile.ZipFile) -> str | None:
    """Why this archive must not be parsed, or None. Reads the central
    directory only (`file_size` is the declared uncompressed size, and a
    reader stops at it)."""
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        return f"{len(infos)} entries"
    if sum(i.file_size for i in infos) > MAX_UNCOMPRESSED_BYTES:
        return "uncompressed size over the limit"
    return None


# OOXML never has a document type declaration, and one is how an XML parser
# is made to read a local file (XXE). The three parsers are configured not to
# resolve entities, but a refusal here does not depend on that staying true
# in a library update. Checked in every encoding XML may be written in.
_DTD_MARKERS = tuple(
    marker.encode(encoding)
    for marker in ("<!DOCTYPE", "<!ENTITY")
    for encoding in ("utf-8", "utf-16-le", "utf-16-be")
)
_SCAN_CHUNK = 1024 * 1024


def _refuse_dtd(zf: zipfile.ZipFile) -> None:
    keep = max(len(m) for m in _DTD_MARKERS) - 1
    for info in zf.infolist():
        if not info.filename.lower().endswith((".xml", ".rels")):
            continue
        tail = b""
        with zf.open(info) as member:
            while chunk := member.read(_SCAN_CHUNK):
                window = tail + chunk
                if any(marker in window for marker in _DTD_MARKERS):
                    raise ConvertError("DTD in a part")
                tail = window[-keep:]


class _Out:
    """The Markdown being built, with the character cap and the notes about
    where it was cut. Blocks are whole paragraphs or table rows, so a cut is
    always at one of those boundaries."""

    def __init__(self) -> None:
        self._blocks: list[tuple[str, str]] = []  # (separator before, text)
        self._size = 0
        self._where = "the start"
        self.full = False
        self.cuts: list[str] = []

    def add(self, text: str, where: str, *, sep: str = "\n\n") -> bool:
        """False once the cap is reached; the caller stops producing."""
        if self.full:
            return False
        size = self._size + len(sep) + len(text) if self._blocks else len(text)
        if size > MAX_CHARS and self._blocks:
            self.full = True
            self.cuts.append(f"it stops after {self._where}, at about {MAX_CHARS:,} characters")
            return False
        self._blocks.append((sep if self._blocks else "", text))
        self._size = size
        self._where = where
        return True

    def table(self, rows: list[list[str]], where: Callable[[int], str]) -> bool:
        """A Markdown table: header and rule together, then a row at a time,
        so a cut never leaves a header with no rule."""
        width = max((len(r) for r in rows), default=0)
        if not width:
            return True
        padded = [r + [""] * (width - len(r)) for r in rows]
        head = _row(padded[0]) + "\n" + _row(["---"] * width)
        if not self.add(head, where(1)):
            return False
        for n, row in enumerate(padded[1:], start=2):
            if not self.add(_row(row), where(n), sep="\n"):
                return False
        return True

    def render(self) -> str:
        body = "".join(sep + text for sep, text in self._blocks)
        if not self.cuts:
            return body
        return f"> Cut: {'; '.join(self.cuts)}.\n\n{body}"


def _cell(value: Any) -> str:
    text = " ".join(str(value).split())
    return text.replace("|", "\\|")


def _row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


# --- Word ------------------------------------------------------------------------

_HEADING = re.compile(r"^Heading (\d)$")


def _docx(path: str) -> str:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = docx.Document(path)
    items = list(document.element.body.iterchildren())
    total = len(items)
    out = _Out()
    for n, element in enumerate(items, start=1):
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "p":
            paragraph = Paragraph(element, document)
            text = paragraph.text.strip()
            if not text:
                continue
            style = getattr(paragraph.style, "name", "") or ""
            heading = _HEADING.match(style)
            if heading:
                text = "#" * min(int(heading.group(1)), 6) + " " + text
            elif style == "Title":
                text = "# " + text
            elif style.startswith("List Bullet"):
                text = "- " + text
            elif style.startswith("List Number"):
                text = "1. " + text
            if not out.add(text, f"paragraph or table {n} of {total}"):
                break
        elif tag == "tbl":
            table = Table(element, document)
            rows = [[_cell(c.text) for c in row.cells] for row in table.rows]
            if not out.table(rows, lambda r, n=n: f"row {r} of table {n} of {total}"):
                break
    return out.render()


# --- Excel -----------------------------------------------------------------------


def _value(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def _xlsx(path: str) -> str:
    import openpyxl

    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        out = _Out()
        names = workbook.sheetnames
        for index, name in enumerate(names):
            if index >= MAX_SHEETS:
                out.cuts.append(f"only the first {MAX_SHEETS} of {len(names)} sheets are shown")
                break
            title = " ".join(name.split())
            if not out.add(f"## {title}", f"the heading of sheet {title}"):
                break
            rows: list[list[str]] = []
            more = False
            for row in workbook[name].iter_rows(values_only=True):
                cells = [_cell(_value(v)) for v in row]
                while cells and not cells[-1]:
                    cells.pop()
                if not cells:
                    continue
                if len(rows) >= MAX_ROWS_PER_SHEET:
                    more = True
                    break
                rows.append(cells)
            if not out.table(rows, lambda r, t=title: f"row {r} of sheet {t}"):
                break
            if more:
                out.cuts.append(
                    f"sheet {title} stops after row {MAX_ROWS_PER_SHEET:,} (it has more)"
                )
        return out.render()
    finally:
        workbook.close()


# --- PowerPoint --------------------------------------------------------------------


def _shape_blocks(shapes: Any, title_shape: Any) -> list[str | list[list[str]]]:
    """Body text, in shape order: a str per text paragraph, a row list per
    table. Groups are walked; pictures and charts have no text here."""
    blocks: list[str | list[list[str]]] = []
    for shape in shapes:
        if title_shape is not None and shape.shape_id == title_shape.shape_id:
            continue
        if getattr(shape, "has_table", False) and shape.has_table:
            blocks.append([[_cell(c.text) for c in row.cells] for row in shape.table.rows])
        elif getattr(shape, "has_text_frame", False) and shape.has_text_frame:
            for paragraph in shape.text_frame.paragraphs:
                text = "".join(run.text for run in paragraph.runs).strip()
                if text:
                    blocks.append(text)
        elif hasattr(shape, "shapes"):
            blocks += _shape_blocks(shape.shapes, None)
    return blocks


def _pptx(path: str) -> str:
    import pptx

    presentation = pptx.Presentation(path)
    slides = list(presentation.slides)
    out = _Out()
    for n, slide in enumerate(slides, start=1):
        where = f"slide {n} of {len(slides)}"
        title_shape = slide.shapes.title
        title = " ".join(title_shape.text_frame.text.split()) if title_shape is not None else ""
        heading = f"## Slide {n}: {title}" if title else f"## Slide {n}"
        if not out.add(heading, f"slide {n - 1} of {len(slides)}"):
            break
        for block in _shape_blocks(slide.shapes, title_shape):
            if isinstance(block, str):
                added = out.add(block, where, sep="\n")
            else:
                added = out.table(block, lambda r, w=where: w)
            if not added:
                break
        else:
            if slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame.text.strip()
                if notes and not out.add(f"Notes:\n{notes}", where):
                    break
            continue
        break
    return out.render()


_CONVERTERS: dict[str, Callable[[str], str]] = {"docx": _docx, "xlsx": _xlsx, "pptx": _pptx}


def convert(kind: str, path: str) -> str:
    """Markdown for the Office file at `path`. Raises ConvertError for a file
    it refuses, and whatever a parser raises for one it cannot read; both
    mean a failed conversion."""
    try:
        with zipfile.ZipFile(path) as zf:
            problem = archive_problem(zf)
            if problem:
                raise ConvertError(f"archive refused: {problem}")
            _refuse_dtd(zf)
    except zipfile.BadZipFile as e:
        raise ConvertError("not a zip archive") from e
    markdown = _CONVERTERS[kind](path)
    if not markdown.strip():
        raise ConvertError("no text in the document")
    return markdown


def _limit_resources() -> None:
    """Best effort: macOS refuses or ignores some of these, and the confinement
    around this process does not depend on them."""
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (_MEMORY_LIMIT_BYTES, _MEMORY_LIMIT_BYTES))
        resource.setrlimit(resource.RLIMIT_FSIZE, (_OUTPUT_LIMIT_BYTES, _OUTPUT_LIMIT_BYTES))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, ValueError, OSError):
        pass


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in _CONVERTERS:
        print("usage: python -m server.convert <docx|xlsx|pptx> <input>", file=sys.stderr)
        return 2
    _limit_resources()
    try:
        markdown = convert(argv[1], argv[2])
    except ConvertError as e:
        print(f"convert: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # a parser's own failure; its message may quote the file
        print(f"convert: {type(e).__name__}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(markdown.encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
