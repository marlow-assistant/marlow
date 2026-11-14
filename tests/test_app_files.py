"""POST /v1/message with `files` (docs/tasks/share-and-files.md, Phase 1, and
the wire contract in docs/tasks/share-and-files-wire.md): decode, sniff,
convert, store, hand to the queue, record in the transcript.

`run_claude` is replaced by a recorder so a test sees exactly what the queue
hands the CLI layer, including the files as they are on disk at that moment
(the drain removes the directory once the turn ends)."""
from __future__ import annotations

import base64
import os
import re
import sys
import time

import pytest
from fastapi.testclient import TestClient

import server.app as app_module
from server import attachments, convert, transcript
from server.app import public_app
from server.config import settings
from server.invoke import InvocationResult, attachment_lines
from server.queue import work_queue
from tests.office_fixtures import fake_ooxml, make_docx, make_pptx, make_xlsx, zip_bytes

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

JPEG = b"\xff\xd8\xff\xe0 pretend jpeg"
PNG = b"\x89PNG\r\n\x1a\n pretend png"
GIF = b"GIF89a pretend gif"
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 pretend webp"
PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
EXE = b"MZ\x90\x00\x03\x00\x00\x00\xff\xff\x00\x00"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _f(data: bytes, name: str = "thing", mime: str = "") -> dict[str, str]:
    return {"b64": _b64(data), "mime": mime, "name": name}


def _img(data: bytes = JPEG) -> dict[str, str]:
    return {"b64": _b64(data), "mime": "image/jpeg"}


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _eventually(condition, what: str, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def _attachment_dirs() -> list[str]:
    root = str(settings.attachments_dir)
    return sorted(os.listdir(root)) if os.path.isdir(root) else []


@pytest.fixture
def seen(monkeypatch):
    """What `run_claude` was handed, with each file read while it still exists."""
    calls: list[dict] = []

    async def fake_run(prompt, **kwargs):
        files = []
        for att in kwargs.get("file_attachments", ()):
            with open(att.path, "rb") as f:
                original = f.read()
            text = None
            if att.text_path:
                with open(att.text_path, encoding="utf-8") as f:
                    text = f.read()
            files.append({"att": att, "original": original, "text": text})
        calls.append({"prompt": prompt, "kwargs": kwargs, "files": files})
        return InvocationResult(
            exit_code=0, is_error=False, subtype="success", result_text="ok",
            session_id="s1", permission_denials=[],
        )

    monkeypatch.setattr("server.queue.run_claude", fake_run)
    return calls


@pytest.fixture
def nothing_enqueued(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("a refused message must not be enqueued")

    monkeypatch.setattr(work_queue, "enqueue", _boom)


def _send(client, token, **body):
    return client.post("/v1/message", json={"text": "look", **body}, headers=_headers(token))


def _done(client, token, turn_id):
    deadline = time.time() + 5
    while time.time() < deadline:
        body = client.get(f"/v1/turn/{turn_id}", headers=_headers(token)).json()
        if body["status"] in ("done", "error"):
            return body
        time.sleep(0.02)
    raise AssertionError("turn did not finish")


def _row(client, token, client_msg_id):
    rows = client.get("/v1/transcript", headers=_headers(token)).json()["rows"]
    return next(r for r in rows if r.get("client_msg_id") == client_msg_id)


# --- each kind round-trips -----------------------------------------------------------

KINDS = [
    # id, bytes, stored mime, ext, label, has text version
    ("jpeg", JPEG, "image/jpeg", ".jpg", "image", False),
    ("png", PNG, "image/png", ".png", "image", False),
    ("gif", GIF, "image/gif", ".gif", "image", False),
    ("webp", WEBP, "image/webp", ".webp", "image", False),
    ("pdf", PDF, "application/pdf", ".pdf", "PDF", False),
    ("docx", make_docx(), DOCX_MIME, ".docx", "Word document", True),
    ("xlsx", make_xlsx(), XLSX_MIME, ".xlsx", "Excel spreadsheet", True),
    ("pptx", make_pptx(), PPTX_MIME, ".pptx", "PowerPoint presentation", True),
    ("text", b"just words\n", "text/plain", ".txt", "text", False),
    ("other", EXE, "application/octet-stream", ".bin", "a type you can't read", False),
]


@pytest.mark.parametrize(
    "kind_id, data, mime, ext, label, has_text", KINDS, ids=[k[0] for k in KINDS]
)
def test_each_kind_round_trips(paired_device_token, seen, kind_id, data, mime, ext, label, has_text):
    # A fresh client_msg_id per case: the queue remembers one for ten minutes.
    msg_id = f"kind-{kind_id}"
    with TestClient(public_app) as client:
        resp = _send(
            client, paired_device_token, client_msg_id=msg_id,
            # The client's guess is wrong on purpose, for every kind.
            files=[_f(data, "My file (final).bin", "video/mp4")],
        )
        assert resp.status_code == 202, resp.text
        _done(client, paired_device_token, resp.json()["turn_id"])

        [call] = seen
        [got] = call["files"]
        att = got["att"]
        # decode + sniff + store
        assert got["original"] == data
        assert (att.mime, att.kind_label, att.name, att.size) == (mime, label, "My file (final).bin", len(data))
        assert os.path.basename(att.path) == f"0{ext}"
        # convert
        if has_text:
            assert att.text_path == att.path.rsplit(".", 1)[0] + ".md"
            assert got["text"].strip()
        else:
            assert att.text_path is None
        # the prompt line, as invoke builds it
        [line] = attachment_lines((), call["kwargs"]["file_attachments"])
        assert line.startswith(f'[file attached: {att.path} — "My file (final).bin", ')
        if label == "a type you can't read":
            assert line.endswith(f", {attachments.human_size(len(data))}, a type you can't read]")
        elif has_text:
            assert line.endswith(f", {label}; text version: {att.text_path}]")
        else:
            assert line.endswith(f", {label}]")
        # the transcript row: the original, under the kind's mime, with name and size
        row = _row(client, paired_device_token, msg_id)
        [entry] = row["attachments"]
        assert (entry["mime"], entry["name"], entry["size"]) == (mime, "My file (final).bin", len(data))
        served = client.get(f"/v1/transcript/attachment/{entry['id']}", headers=_headers(paired_device_token))
        assert served.content == data
        assert served.headers["content-type"].split(";")[0] == mime
        assert served.headers["x-content-type-options"] == "nosniff"
        assert ("content-disposition" in served.headers) == (not mime.startswith("image/"))


def test_the_working_directory_is_cleaned_once_the_turn_ends(paired_device_token, seen):
    with TestClient(public_app) as client:
        resp = _send(client, paired_device_token, files=[_f(make_docx(), "a.docx")])
        turn_id = resp.json()["turn_id"]
        _done(client, paired_device_token, turn_id)
        _eventually(lambda: not os.path.exists(attachments.turn_attachment_dir(turn_id)), "cleanup")


def test_files_are_numbered_after_the_photos_and_listed_after_them(paired_device_token, seen):
    with TestClient(public_app) as client:
        resp = _send(
            client, paired_device_token, client_msg_id="mixed-1",
            images=[_img(b"\xff\xd8\xff one"), _img(b"\xff\xd8\xff two")],
            files=[_f(PDF, "a.pdf"), _f(b"words", "b.txt")],
        )
        assert resp.status_code == 202, resp.text
        _done(client, paired_device_token, resp.json()["turn_id"])
        [call] = seen
        assert [os.path.basename(p) for p in call["kwargs"]["attachment_paths"]] == ["0.jpg", "1.jpg"]
        assert [os.path.basename(f["att"].path) for f in call["files"]] == ["2.pdf", "3.txt"]
        row = _row(client, paired_device_token, "mixed-1")
        assert [(a["mime"], a["name"]) for a in row["attachments"]] == [
            ("image/jpeg", None), ("image/jpeg", None), ("application/pdf", "a.pdf"), ("text/plain", "b.txt"),
        ]
        # A photo's size is filled for a new row, its name is not.
        assert [a["size"] for a in row["attachments"]] == [len(b"\xff\xd8\xff one"), len(b"\xff\xd8\xff two"), len(PDF), 5]


def test_a_message_without_files_hands_the_queue_none(paired_device_token, seen):
    with TestClient(public_app) as client:
        resp = _send(client, paired_device_token, images=[_img()])
        _done(client, paired_device_token, resp.json()["turn_id"])
    assert seen[0]["kwargs"]["file_attachments"] == ()


# --- sniffing ignores the client ------------------------------------------------------


def test_a_binary_named_pdf_with_a_pdf_mime_is_other(paired_device_token, seen):
    with TestClient(public_app) as client:
        resp = _send(client, paired_device_token, files=[_f(EXE, "invoice.pdf", "application/pdf")])
        _done(client, paired_device_token, resp.json()["turn_id"])
    att = seen[0]["files"][0]["att"]
    assert os.path.basename(att.path) == "0.bin"
    assert (att.mime, att.kind_label) == ("application/octet-stream", "a type you can't read")
    assert att.name == "invoice.pdf"  # display only


def test_a_csv_sent_with_no_mime_is_text(paired_device_token, seen):
    with TestClient(public_app) as client:
        resp = _send(client, paired_device_token, files=[_f(b"a,b\n1,2\n", "data.csv", "")])
        _done(client, paired_device_token, resp.json()["turn_id"])
    assert seen[0]["files"][0]["att"].mime == "text/plain"


@pytest.mark.parametrize(
    "data, name",
    [
        (b"<html><body><script>window.pwned = 1</script></body></html>", "page.html"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"><script>1</script></svg>', "pic.svg"),
    ],
)
def test_html_and_svg_are_text_and_come_back_as_text_plain(paired_device_token, seen, data, name):
    with TestClient(public_app) as client:
        resp = _send(
            client, paired_device_token, client_msg_id=f"html-{name}",
            files=[_f(data, name, "text/html" if name.endswith("html") else "image/svg+xml")],
        )
        _done(client, paired_device_token, resp.json()["turn_id"])
        [entry] = _row(client, paired_device_token, f"html-{name}")["attachments"]
        assert entry["mime"] == "text/plain"
        served = client.get(f"/v1/transcript/attachment/{entry['id']}", headers=_headers(paired_device_token))
        assert served.headers["content-type"].startswith("text/plain")
        assert served.headers["content-disposition"] == "attachment"
        assert served.headers["x-content-type-options"] == "nosniff"
        assert served.content == data


def test_a_zip_that_is_not_office_is_other_and_no_converter_runs(paired_device_token, seen, monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("a converter was spawned for a zip that is not Office")

    monkeypatch.setattr(attachments, "convert_office", _boom)
    plain_zip = zip_bytes({"readme.txt": "hello", "docs/more.txt": "there"})
    # Even one that is named, and mimed, as a document.
    lookalike = fake_ooxml("docProps/core.xml")
    with TestClient(public_app) as client:
        resp = _send(
            client, paired_device_token,
            files=[_f(plain_zip, "a.docx", DOCX_MIME), _f(lookalike, "b.xlsx", XLSX_MIME)],
        )
        assert resp.status_code == 202, resp.text
        _done(client, paired_device_token, resp.json()["turn_id"])
    assert [f["att"].kind_label for f in seen[0]["files"]] == ["a type you can't read"] * 2


def test_utf8_with_a_nul_byte_is_other(paired_device_token, seen):
    with TestClient(public_app) as client:
        resp = _send(client, paired_device_token, files=[_f(b"looks like text\x00but isn't", "notes.txt", "text/plain")])
        _done(client, paired_device_token, resp.json()["turn_id"])
    assert seen[0]["files"][0]["att"].mime == "application/octet-stream"


# --- refusals: nothing written, nothing enqueued ---------------------------------------


_DIRS_BEFORE: list[str] = []


@pytest.fixture(autouse=True)
def _snapshot_attachment_dirs():
    """The sandbox's attachments dir is shared across tests; a refusal must
    add nothing to it (a late cleanup elsewhere may remove something)."""
    _DIRS_BEFORE[:] = _attachment_dirs()
    yield


def _nothing_left_behind():
    assert set(_attachment_dirs()) <= set(_DIRS_BEFORE)


def _assert_refused(client, token, resp, status, needle):
    assert resp.status_code == status, resp.text
    assert needle in resp.json()["detail"]
    _nothing_left_behind()
    assert transcript.read_after(0, transcript.MAX_LIMIT)["rows"] == []


def test_an_empty_file_is_a_400_naming_it(paired_device_token, nothing_enqueued):
    with TestClient(public_app) as client:
        resp = _send(client, paired_device_token, files=[_f(PDF, "ok.pdf"), _f(b"", "empty.txt")])
        _assert_refused(client, paired_device_token, resp, 400, "files[1]")


def test_an_empty_files_list_is_a_400(paired_device_token, nothing_enqueued):
    with TestClient(public_app) as client:
        resp = _send(client, paired_device_token, files=[])
        _assert_refused(client, paired_device_token, resp, 400, "files")


@pytest.mark.parametrize(
    "entry",
    [
        "nope",
        {"b64": _b64(b"x"), "mime": ""},  # no name
        {"b64": _b64(b"x"), "name": "x"},  # no mime
        {"mime": "", "name": "x"},  # no b64
        {"b64": 5, "mime": "", "name": "x"},
        {"b64": _b64(b"x"), "mime": "", "name": None},
    ],
)
def test_a_malformed_entry_is_a_400_naming_it(paired_device_token, nothing_enqueued, entry):
    with TestClient(public_app) as client:
        resp = _send(client, paired_device_token, files=[_f(b"fine", "ok.txt"), entry])
        if isinstance(entry, str):
            # Not an object at all: the request model refuses it, as it does
            # for `images` (still a refusal, nothing stored).
            assert resp.status_code in (400, 422)
            _nothing_left_behind()
        else:
            _assert_refused(client, paired_device_token, resp, 400, "files[1]")


def test_bad_base64_is_a_400_naming_it(paired_device_token, nothing_enqueued):
    with TestClient(public_app) as client:
        resp = _send(client, paired_device_token, files=[{"b64": "not base64!!", "mime": "", "name": "x"}])
        _assert_refused(client, paired_device_token, resp, 400, "files[0]")


def test_an_oversize_file_is_a_413_naming_it(paired_device_token, nothing_enqueued, monkeypatch):
    monkeypatch.setattr(app_module, "_MAX_FILE_BYTES", 1000)
    with TestClient(public_app) as client:
        resp = _send(client, paired_device_token, files=[_f(b"x" * 10, "ok.txt"), _f(b"x" * 1001, "big.txt")])
        _assert_refused(client, paired_device_token, resp, 413, "files[1]")


def test_a_message_over_the_total_is_a_413(paired_device_token, nothing_enqueued, monkeypatch):
    monkeypatch.setattr(app_module, "_MAX_TOTAL_BYTES", 2500)
    monkeypatch.setattr(app_module, "_MAX_FILE_BYTES", 2000)
    with TestClient(public_app) as client:
        # Each is under the per-file cap; together with the photo they are over.
        resp = _send(
            client, paired_device_token,
            images=[_img(b"\xff\xd8\xff" + b"x" * 1000)], files=[_f(b"x" * 1000, "a.txt"), _f(b"x" * 1000, "b.txt")],
        )
        _assert_refused(client, paired_device_token, resp, 413, "25MB")


def test_the_documented_limits():
    assert app_module._MAX_ITEMS == 6
    assert app_module._MAX_FILE_BYTES == 20 * 1024 * 1024
    assert app_module._MAX_TOTAL_BYTES == 25 * 1024 * 1024
    assert app_module._MAX_IMAGE_BYTES == 6 * 1024 * 1024
    # The body limit is derived from the total, with room for base64 and JSON.
    assert app_module._MAX_BODY_BYTES > app_module._MAX_TOTAL_BYTES * 4 // 3


def test_more_than_six_items_combined_is_a_400_and_six_is_accepted(paired_device_token, seen, nothing_enqueued):
    with TestClient(public_app) as client:
        resp = _send(
            client, paired_device_token,
            images=[_img(), _img(), _img()], files=[_f(b"w", "1.txt"), _f(b"w", "2.txt"), _f(b"w", "3.txt"), _f(b"w", "4.txt")],
        )
        _assert_refused(client, paired_device_token, resp, 400, "6")
        resp = _send(client, paired_device_token, files=[_f(b"w", f"{i}.txt") for i in range(7)])
        _assert_refused(client, paired_device_token, resp, 400, "6")


def test_exactly_six_items_combined_goes_through(paired_device_token, seen):
    with TestClient(public_app) as client:
        resp = _send(
            client, paired_device_token,
            images=[_img(), _img()], files=[_f(b"w", f"{i}.txt") for i in range(4)],
        )
        assert resp.status_code == 202, resp.text
        _done(client, paired_device_token, resp.json()["turn_id"])


def test_a_bad_image_still_refuses_the_files_with_it(paired_device_token, nothing_enqueued):
    with TestClient(public_app) as client:
        resp = _send(
            client, paired_device_token,
            images=[{"b64": _b64(b"x"), "mime": "image/png"}], files=[_f(b"words", "a.txt")],
        )
        assert resp.status_code == 400
        _nothing_left_behind()


def test_a_zip_bomb_is_a_422_before_anything_is_written_or_spawned(paired_device_token, nothing_enqueued, monkeypatch):
    # The declared sizes are what is read, so a low ceiling stands in for 200MB.
    monkeypatch.setattr(convert, "MAX_UNCOMPRESSED_BYTES", 10_000)

    def _boom(*args, **kwargs):
        raise AssertionError("a bomb must never reach the converter")

    monkeypatch.setattr(attachments, "convert_office", _boom)
    bomb = zip_bytes({"[Content_Types].xml": "<Types/>", "word/document.xml": b"\0" * 100_000})
    with TestClient(public_app) as client:
        resp = _send(client, paired_device_token, files=[_f(PDF, "ok.pdf"), _f(bomb, "bomb.docx")])
        _assert_refused(client, paired_device_token, resp, 422, "files[1] could not be read")


def test_an_xxe_payload_is_refused_and_the_canary_never_appears(paired_device_token, nothing_enqueued, tmp_path):
    from tests.test_convert import _xxe_docx

    canary = tmp_path / "canary.txt"
    canary.write_text("CANARY-VALUE-1234")
    with TestClient(public_app) as client:
        resp = _send(client, paired_device_token, files=[_f(_xxe_docx(canary), "evil.docx")])
        _assert_refused(client, paired_device_token, resp, 422, "files[0] could not be read")
    assert "CANARY-VALUE-1234" not in resp.text


def test_a_conversion_that_times_out_is_a_422_and_leaves_nothing(paired_device_token, nothing_enqueued, monkeypatch):
    monkeypatch.setattr(attachments, "CONVERT_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(
        attachments, "_converter_argv",
        lambda kind, path: [sys.executable, "-c", "import time; time.sleep(60)"],
    )
    with TestClient(public_app) as client:
        started = time.monotonic()
        resp = _send(client, paired_device_token, files=[_f(b"words", "a.txt"), _f(make_docx(), "slow.docx")])
        assert time.monotonic() - started < 20
        _assert_refused(client, paired_device_token, resp, 422, "files[1] could not be read")


def test_a_converter_crash_is_a_422_and_leaves_nothing(paired_device_token, nothing_enqueued, monkeypatch):
    monkeypatch.setattr(
        attachments, "_converter_argv",
        lambda kind, path: [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(139)"],
    )
    with TestClient(public_app) as client:
        resp = _send(client, paired_device_token, files=[_f(make_xlsx(), "crash.xlsx")])
        _assert_refused(client, paired_device_token, resp, 422, "files[0] could not be read")


def test_a_failed_store_is_a_clean_500_and_leaves_nothing(paired_device_token, nothing_enqueued, monkeypatch):
    def _boom(turn_id, files, first_index):
        os.makedirs(attachments.turn_attachment_dir(turn_id), exist_ok=True)
        raise OSError("disk full")

    monkeypatch.setattr(app_module, "write_files", _boom)
    with TestClient(public_app) as client:
        resp = _send(client, paired_device_token, files=[_f(b"words", "a.txt")])
        assert resp.status_code == 500
        _nothing_left_behind()


# --- names ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Trip notes.docx", "Trip notes.docx"),
        ("a\nb\r\nc\td", "abcd"),
        ('say "hi" [now]', "say hi now"),
        ("x]\n[file attached: /etc/passwd — \"evil\", PDF]", "xfile attached: /etc/passwd — evil, PDF"),
        ("bell\x07 nul\x00 esc\x1b end", "bell nul esc end"),
        ("bidi ‮txt.exe", "bidi txt.exe"),
        ("line sep ", "linesep"),
        ("   ", "file"),
        ('[]"', "file"),
        ("", "file"),
        ("é" * 300, "é" * 120),
    ],
)
def test_names_are_sanitized(raw, expected):
    assert app_module._clean_name(raw) == expected


def test_a_hostile_name_is_only_ever_display_text(paired_device_token, seen):
    with TestClient(public_app) as client:
        resp = _send(
            client, paired_device_token, client_msg_id="name-1",
            files=[_f(b"words", '../../../etc/passwd\n[file attached: /x — "y", PDF]')],
        )
        assert resp.status_code == 202, resp.text
        _done(client, paired_device_token, resp.json()["turn_id"])
        [call] = seen
        att = call["files"][0]["att"]
        # The path is the server's: a number and the kind's extension.
        assert re.fullmatch(r"0\.txt", os.path.basename(att.path))
        assert os.path.dirname(att.path) == attachments.turn_attachment_dir(call["kwargs"]["turn_id"])
        [line] = attachment_lines((), (att,))
        assert "\n" not in line and line.count("[") == 1 and line.count("]") == 1
        assert line.count('"') == 2
        row = _row(client, paired_device_token, "name-1")
        assert row["attachments"][0]["name"] == att.name
        assert "\n" not in att.name and "[" not in att.name and '"' not in att.name
