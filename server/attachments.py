"""Uploaded attachments: photos (docs/tasks/ios-image-input.md) and files
(docs/tasks/share-and-files.md).

Storage layout: `<settings.attachments_dir>/<turn_id>/<n>.<ext>`, n counting up
from 0 in the order the client attached them, photos first and then files (up
to server/app.py's _MAX_ITEMS together). A photo is always `.jpg`; a file's
extension comes from its sniffed kind (see `sniff`), never from its name. An
Office file also gets `<n>.md`, its text, written by the server from what
server/convert.py printed. server/app.py writes the files
BEFORE enqueueing the turn, so they exist by the time the worker can reach them;
server/queue.py's drain loop removes it once the turn reaches a terminal
state, unconditional on status — an ERRORed turn leaves the same bytes behind
as a DONE one.

Deliberately NOT under `settings.data_dir`: that directory is in
`boundaries.SECRET_PATHS` (hard-denied for Read, and hidden behind a bwrap
tmpfs by invoke.py), so a file placed there would be structurally unreadable
by the CLI — a "file not found" that survives every permission fix. See
`boundaries.ATTACHMENT_ROOT` for the read-allow this directory gets instead.

Size and count validation are HTTP-level policy decisions and live in
server/app.py where the wire request is decoded; this module knows the kind of
a file (from its bytes), the path, the write, the conversion, and the cleanup.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import signal
import subprocess
import sys
import sysconfig
import tempfile
import time
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass

from server.config import settings
from server.convert import archive_problem
from server.log import get_logger

logger = get_logger(__name__)

# A restart drops in-flight turns (server/queue.py's own KNOWN GAP comments on
# the reauth-pause path document the same thing for a different reason), so a
# turn's attachment directory can be orphaned with no in-memory Turn left to
# ever call cleanup_attachment() for it. Swept once at queue startup rather
# than relied on to never happen.
_ORPHAN_MAX_AGE_SECONDS = 24 * 3600


def turn_attachment_dir(turn_id: str) -> str:
    return os.path.join(str(settings.attachments_dir), turn_id)


# --- what a file is, from its bytes ---------------------------------------------


@dataclass(frozen=True)
class Kind:
    """What `sniff` decided. `mime` is what the transcript stores and the
    download endpoint serves; `label` is the server's word for it in the
    prompt; `ext` is the stored extension. `converter` names the
    server/convert.py reader for an Office kind, None for everything else."""

    mime: str
    label: str
    ext: str
    converter: str | None = None


OTHER_LABEL = "a type you can't read"
OTHER = Kind("application/octet-stream", OTHER_LABEL, ".bin")
TEXT = Kind("text/plain", "text", ".txt")
PDF = Kind("application/pdf", "PDF", ".pdf")
_IMAGES = {
    "jpeg": Kind("image/jpeg", "image", ".jpg"),
    "png": Kind("image/png", "image", ".png"),
    "gif": Kind("image/gif", "image", ".gif"),
    "webp": Kind("image/webp", "image", ".webp"),
}
# (main part that names the format, kind). `[Content_Types].xml` must be there
# too: a zip with only a part of that name is not an Office file.
_OFFICE = (
    ("word/document.xml", Kind(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "Word document", ".docx", "docx")),
    ("xl/workbook.xml", Kind(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "Excel spreadsheet", ".xlsx", "xlsx")),
    ("ppt/presentation.xml", Kind(
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "PowerPoint presentation", ".pptx", "pptx")),
)

# zipfile builds a ZipInfo for every entry of the central directory it is
# handed, so a hostile archive with millions of entries costs memory in the
# server process before anything else can object. The directory's size is in
# the end record at the tail, so it is bounded from there, ahead of ZipFile.
# Far above any real document (5,000 entries is the refusal limit proper).
MAX_CENTRAL_DIRECTORY_BYTES = 4 * 1024 * 1024
_ZIP_LOCAL_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06")
_EOCD_SIGNATURE = b"PK\x05\x06"
_EOCD_SIZE = 22
_MAX_EOCD_COMMENT = 65535


def _central_directory_size(data: bytes) -> int | None:
    tail = data[-(_MAX_EOCD_COMMENT + _EOCD_SIZE):]
    at = tail.rfind(_EOCD_SIGNATURE)
    if at < 0 or len(tail) < at + _EOCD_SIZE:
        return None
    return int.from_bytes(tail[at + 12:at + 16], "little")


def _office_kind(data: bytes) -> Kind | None:
    """The Office kind of a zip, from its central directory ALONE: names are
    listed, no member is decompressed and no XML is parsed in this process.
    The parsers that read hostile content run confined (`_converter_argv`)."""
    if not data.startswith(_ZIP_LOCAL_SIGNATURES):
        return None
    size = _central_directory_size(data)
    if size is None or size > MAX_CENTRAL_DIRECTORY_BYTES:
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
    except (zipfile.BadZipFile, OSError, ValueError, NotImplementedError):
        return None
    if "[Content_Types].xml" not in names:
        return None
    return next((kind for part, kind in _OFFICE if part in names), None)


def _is_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.removeprefix(b"\xef\xbb\xbf").decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def sniff(data: bytes) -> Kind:
    """The kind of a file, from its bytes alone: the first match of images,
    PDF, Office, text, else Other. The client's mime and the file's name are
    never inputs, so a binary named `.pdf` is Other and an HTML file is text.
    Pure: no I/O, and nothing inside an archive is read."""
    if data.startswith(b"\xff\xd8\xff"):
        return _IMAGES["jpeg"]
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return _IMAGES["png"]
    if data.startswith((b"GIF87a", b"GIF89a")):
        return _IMAGES["gif"]
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _IMAGES["webp"]
    if data.startswith(b"%PDF-"):
        return PDF
    office = _office_kind(data)
    if office is not None:
        return office
    return TEXT if _is_text(data) else OTHER


def archive_refusal(data: bytes) -> str | None:
    """Why an Office file must not be handed to the converter, or None: the
    zip-bomb limits, checked on the central directory before anything is
    written or spawned. Only for bytes `sniff` called Office, whose directory
    it has already bounded."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return archive_problem(zf)
    except (zipfile.BadZipFile, OSError, ValueError, NotImplementedError):
        return "unreadable archive"


@dataclass(frozen=True)
class FileAttachment:
    """One file of a turn, as the prompt and the cleanup see it. `name` is
    the user's display name, already sanitized (server/app.py); `kind_label`
    and `mime` are the server's, from `sniff`; `text_path` is the `.md` of an
    Office file; `size` is the original's bytes."""

    path: str
    name: str
    kind_label: str
    size: int
    text_path: str | None
    mime: str


def human_size(size: int) -> str:
    """`2.1 MB`, `0.3 KB`: one decimal, KB under a megabyte."""
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def write_attachments(turn_id: str, images: list[bytes]) -> tuple[str, ...]:
    """Writes the decoded images as 0.jpg, 1.jpg, ... and returns their paths,
    in order.

    Caller's job to validate size/MIME first (server/app.py) — this only knows
    how to place bytes on disk, 0700 like every other private state directory
    on this box. One directory per turn, so cleanup and the orphan sweep stay
    per-turn however many images it holds.
    """
    turn_dir = turn_attachment_dir(turn_id)
    os.makedirs(turn_dir, mode=0o700, exist_ok=True)
    paths: list[str] = []
    for i, data in enumerate(images):
        path = os.path.join(turn_dir, f"{i}.jpg")
        with open(path, "wb") as f:
            f.write(data)
        paths.append(path)
    return tuple(paths)


def write_files(
    turn_id: str, files: Sequence[tuple[bytes, Kind]], first_index: int
) -> tuple[str, ...]:
    """Writes each file as `<n>.<ext of its kind>`, n continuing from the
    photos (`first_index` is how many there are), and returns the paths in
    order. Same directory as the photos, same 0700, same caller-validates
    contract as `write_attachments`."""
    turn_dir = turn_attachment_dir(turn_id)
    os.makedirs(turn_dir, mode=0o700, exist_ok=True)
    paths: list[str] = []
    for n, (data, kind) in enumerate(files, start=first_index):
        path = os.path.join(turn_dir, f"{n}{kind.ext}")
        with open(path, "wb") as f:
            f.write(data)
        paths.append(path)
    return tuple(paths)


def cleanup_attachment(turn_id: str) -> None:
    """Removes the turn's attachment directory, if any.

    Never raises — mirrors identity_git.commit_after_turn's contract, and
    server/queue.py wraps the call anyway rather than resting the drain loop
    on that promise alone (the lesson from that module: an exception escaping
    here would kill the only consumer task in the process).
    """
    turn_dir = turn_attachment_dir(turn_id)
    try:
        shutil.rmtree(turn_dir)
    except FileNotFoundError:
        pass
    except OSError:
        logger.exception("failed to clean up attachment dir %s", turn_dir)


def sweep_orphaned_attachments(now: float | None = None) -> int:
    """Removes attachment dirs older than 24h. Returns the count removed.

    What an in-flight turn's directory becomes if the server restarts before
    the drain loop's own cleanup runs — the in-memory Turn (and with it, the
    only thing that would have called cleanup_attachment) is gone the moment
    the process is, so nothing left in `_turns` ever cleans up a directory
    written just before the restart.
    """
    root = str(settings.attachments_dir)
    if not os.path.isdir(root):
        return 0
    now = now if now is not None else time.time()
    removed = 0
    for name in os.listdir(root):
        path = os.path.join(root, name)
        try:
            age = now - os.path.getmtime(path)
        except OSError:
            continue
        if age <= _ORPHAN_MAX_AGE_SECONDS:
            continue
        try:
            shutil.rmtree(path)
            removed += 1
        except OSError:
            logger.exception("failed to sweep orphaned attachment dir %s", path)
    if removed:
        logger.info("swept %d orphaned attachment dir(s) older than 24h", removed)
    return removed


# --- reading an Office file, confined -------------------------------------------------


class ConversionError(Exception):
    """The converter could not make text of this file. The message names the
    class of failure only; nothing from the document is in it."""


# Module constants so a test can swap them (a fake converter that sleeps).
CONVERT_TIMEOUT_SECONDS = 30
# What the server will read back. The converter cuts its own output at about
# 200,000 characters (under 1MB, even all in four-byte ones), so more than this
# is a converter gone wrong, not a long document.
MAX_CONVERTED_BYTES = 2 * 1024 * 1024
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
_warned_unconfined = False
# What server/convert.py writes to stderr on a refusal or a parse failure:
# a fixed phrase or a class name, so only a line of this shape is ever logged.
_SAFE_STDERR = re.compile(r"^convert: [\w .:-]{1,80}$")


def _converter_argv(kind: str, input_path: str) -> list[str]:
    """The command that reads one Office file: `python -m server.convert
    <kind> <input>`, wrapped in bubblewrap when `settings.bwrap_enabled`.

    This namespace is the OPPOSITE of invoke._confinement_prefix's. That one
    binds `/` and hides the secrets, because the CLI must reach the whole box
    but not them. This one binds almost nothing: a read-only `/usr`, the
    interpreter, this checkout and the one input file, a tmpfs for `/tmp`, and
    no network (`--unshare-all`). `~/.marlow`, the device tokens, the APNs key
    and the other turns' files do not exist inside it, so a parser exploited
    by a hostile document has nothing to read, nowhere to write and no one to
    tell. The environment is cleared for the same reason.

    Without bwrap the same command runs bare, and says so once: the box has
    opted out of confinement (deploy/README.md §11), and this is one of the
    things it gives up."""
    command = [sys.executable, "-s", "-m", "server.convert", kind, input_path]
    if not settings.bwrap_enabled:
        global _warned_unconfined
        if not _warned_unconfined:
            _warned_unconfined = True
            logger.warning(
                "converting an Office file unconfined: bwrap is disabled, so the "
                "parsers run with the server's own filesystem access"
            )
        return command

    real = os.path.realpath
    interpreter_trees = {real(sys.base_prefix), real(sys.prefix)}
    site_packages = real(sysconfig.get_paths()["purelib"])
    if not any(site_packages == t or site_packages.startswith(t + os.sep) for t in interpreter_trees):
        interpreter_trees.add(site_packages)
    system_dirs = ("/lib", "/lib64", "/bin", "/sbin", "/etc/ld.so.cache", "/etc/alternatives")
    read_only = [
        *(a for path in system_dirs for a in ("--ro-bind-try", path, path)),
        *(a for path in sorted(interpreter_trees) for a in ("--ro-bind-try", path, path)),
        "--ro-bind", _REPO_ROOT, _REPO_ROOT,
        "--ro-bind", input_path, input_path,
    ]
    # The fresh mounts go FIRST: bwrap applies its arguments in order, so a
    # `--tmpfs /tmp` after the binds would hide any bound path under /tmp
    # (seen on a Linux VM, 2026-11-14: every conversion exited 1 with the
    # checkout under /tmp, because the interpreter's own tree vanished).
    return [
        settings.bwrap_bin, "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
        "--ro-bind", "/usr", "/usr", *read_only,
        "--setenv", "PYTHONPATH", _REPO_ROOT, "--setenv", "HOME", "/tmp",
        "--chdir", "/tmp", "--", *command,
    ]


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    """The converter's whole process group, not just the child: the group is
    its own (`start_new_session`), so this reaches anything it started."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def convert_office(converter: str, input_path: str) -> str:
    """Runs the converter on `input_path` and writes its Markdown beside it as
    `<n>.md`; returns that path. Raises ConversionError on a timeout, a
    non-zero exit, empty or oversize or non-UTF-8 output, and leaves no `.md`.

    The server captures stdout into a file it opened, not a pipe it has to
    buffer, and reads at most MAX_CONVERTED_BYTES + 1 of it. A separate
    process group, so a timeout kills what the converter started too. Standard
    input is /dev/null and the environment is PATH and PYTHONPATH alone: the
    server's own (device tokens in a path, API keys) never reaches a parser,
    confined or not."""
    env = {"PATH": os.environ.get("PATH") or "/usr/bin:/bin", "PYTHONPATH": _REPO_ROOT}
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(
            _converter_argv(converter, input_path),
            stdin=subprocess.DEVNULL, stdout=out, stderr=err,
            env=env, cwd=_REPO_ROOT, start_new_session=True,
        )
        try:
            proc.wait(timeout=CONVERT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            raise ConversionError("timed out") from None
        err.seek(0)
        last = (err.read(4096).decode("utf-8", "replace").strip().splitlines() or [""])[-1]
        if proc.returncode != 0:
            if _SAFE_STDERR.match(last):
                logger.warning("office conversion failed: %s", last)
            raise ConversionError(f"exit status {proc.returncode}")
        out.seek(0)
        raw = out.read(MAX_CONVERTED_BYTES + 1)
    if len(raw) > MAX_CONVERTED_BYTES:
        raise ConversionError("output over the limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ConversionError("output not UTF-8") from None
    if not text.strip():
        raise ConversionError("no output")
    text_path = os.path.splitext(input_path)[0] + ".md"
    with open(text_path, "w", encoding="utf-8") as f:
        f.write(text)
    return text_path
