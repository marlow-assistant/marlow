"""server/attachments.sniff: the kind of a file, from its bytes alone
(docs/tasks/share-and-files.md, "Kinds, by sniffing"). The client's mime and
the file's name never reach this function, so none of these tests can pass
them."""
from __future__ import annotations

import zipfile

import pytest

from server import attachments
from server.attachments import sniff
from tests.office_fixtures import fake_ooxml, zip_bytes

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


@pytest.mark.parametrize(
    "data, mime, ext",
    [
        (b"\xff\xd8\xff\xe0 jpeg", "image/jpeg", ".jpg"),
        (b"\x89PNG\r\n\x1a\n rest", "image/png", ".png"),
        (b"GIF87a rest", "image/gif", ".gif"),
        (b"GIF89a rest", "image/gif", ".gif"),
        (b"RIFF\x24\x00\x00\x00WEBPVP8 rest", "image/webp", ".webp"),
    ],
)
def test_image_signatures(data, mime, ext):
    kind = sniff(data)
    assert (kind.mime, kind.ext, kind.label) == (mime, ext, "image")
    assert kind.converter is None


def test_a_riff_file_that_is_not_webp_is_not_an_image():
    # A WAV is RIFF too; "RIFF" alone must not read as WebP.
    assert sniff(b"RIFF\x24\x00\x00\x00WAVEfmt \x00\x00").mime == "application/octet-stream"


def test_pdf():
    kind = sniff(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    assert (kind.mime, kind.ext, kind.label) == ("application/pdf", ".pdf", "PDF")
    assert kind.converter is None


@pytest.mark.parametrize(
    "main_part, mime, ext, label, converter",
    [
        ("word/document.xml", DOCX_MIME, ".docx", "Word document", "docx"),
        ("xl/workbook.xml", XLSX_MIME, ".xlsx", "Excel spreadsheet", "xlsx"),
        ("ppt/presentation.xml", PPTX_MIME, ".pptx", "PowerPoint presentation", "pptx"),
    ],
)
def test_office_files_are_found_by_the_central_directory(main_part, mime, ext, label, converter):
    kind = sniff(fake_ooxml(main_part))
    assert (kind.mime, kind.ext, kind.label, kind.converter) == (mime, ext, label, converter)


def test_sniffing_an_office_file_never_decompresses_or_parses_anything(monkeypatch):
    # The server process holds the device tokens: whatever a hostile file
    # contains, deciding its kind must not read a single member.
    def _boom(*args, **kwargs):
        raise AssertionError("sniff read inside the archive")

    data = fake_ooxml("word/document.xml")  # built first: writing uses open()
    monkeypatch.setattr(zipfile.ZipFile, "open", _boom)
    monkeypatch.setattr(zipfile.ZipFile, "read", _boom)
    monkeypatch.setattr(zipfile.ZipFile, "extractall", _boom)
    assert sniff(data).converter == "docx"


@pytest.mark.parametrize(
    "data",
    [
        zip_bytes({"hello.txt": "hi", "notes/two.txt": "there"}),
        # The content-types part alone, or a main part alone, is not Office.
        fake_ooxml("word/document.xml", content_types=False),
        fake_ooxml("docProps/core.xml"),
        b"PK\x03\x04\x14\x00\x00\x00 truncated, not a real archive",
    ],
)
def test_a_zip_that_is_not_office_is_other(data):
    kind = sniff(data)
    assert (kind.mime, kind.ext) == ("application/octet-stream", ".bin")
    assert kind.converter is None


def test_other_has_the_phrase_the_identity_keys_on():
    assert sniff(b"\x00\x01\x02 binary").label == "a type you can't read"


@pytest.mark.parametrize(
    "data",
    [
        b"plain words\n",
        "naïve café — ünïcode".encode(),
        b"\xef\xbb\xbfwith a byte order mark\n",
        b"name,qty\nbolts,4\nnuts,9\n",  # a CSV: text, whatever it was called
        b"<html><script>alert(1)</script></html>",
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
    ],
)
def test_text_is_decided_by_content(data):
    kind = sniff(data)
    assert (kind.mime, kind.ext, kind.label) == ("text/plain", ".txt", "text")


@pytest.mark.parametrize(
    "data",
    [
        b"looks fine\x00but has a NUL",
        b"\xff\xfeU\x00T\x00F\x00-\x001\x006\x00",  # UTF-16 with a BOM
        b"\xc3\x28 not valid utf-8",
        b"MZ\x90\x00\x03\x00\x00\x00",  # an executable
    ],
)
def test_binary_is_other(data):
    assert sniff(data).mime == "application/octet-stream"


def test_an_executable_named_pdf_is_other_because_only_bytes_are_read():
    assert sniff(b"MZ\x90\x00" + b"\x00" * 64).ext == ".bin"


def test_signature_order_is_images_then_pdf_then_office_then_text():
    # A PDF that is also valid UTF-8 is a PDF, not text.
    assert sniff(b"%PDF-1.4\nplain ascii body").label == "PDF"
    # PNG bytes with a NUL-free tail are still an image.
    assert sniff(b"\x89PNG\r\n\x1a\n").label == "image"


def test_an_absurd_central_directory_is_other_and_never_opened(monkeypatch):
    # zipfile reads a whole central directory into memory, so a hostile one
    # is bounded from the end-of-directory record before ZipFile sees it.
    opened = []
    real = zipfile.ZipFile

    def _spy(*args, **kwargs):
        opened.append(1)
        return real(*args, **kwargs)

    data = fake_ooxml("word/document.xml")
    monkeypatch.setattr(attachments.zipfile, "ZipFile", _spy)
    monkeypatch.setattr(attachments, "MAX_CENTRAL_DIRECTORY_BYTES", 64)
    kind = sniff(data)
    assert kind.mime == "application/octet-stream"
    assert not opened
