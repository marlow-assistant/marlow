"""server/attachments.py's conversion half: the confined argv, and the runner
that turns an Office file into `<n>.md` or a ConversionError
(docs/tasks/share-and-files.md, Phase 1, "Conversion")."""
from __future__ import annotations

import dataclasses
import logging
import os
import sys
import time

import pytest

from server import attachments
from server.config import settings as real_settings
from tests.office_fixtures import make_docx

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def _settings(monkeypatch, tmp_path, **overrides):
    monkeypatch.setattr(
        attachments, "settings",
        dataclasses.replace(real_settings, attachments_dir=tmp_path / "attachments", **overrides),
    )


# --- the bubblewrap argv -----------------------------------------------------------


def _bwrap_argv(monkeypatch, tmp_path, kind="docx"):
    _settings(monkeypatch, tmp_path, bwrap_enabled=True, bwrap_bin="/usr/bin/bwrap")
    path = str(tmp_path / "attachments" / "turn-1" / "0.docx")
    return attachments._converter_argv(kind, path), path


def test_the_confined_argv_runs_the_converter_in_an_empty_namespace(monkeypatch, tmp_path):
    argv, path = _bwrap_argv(monkeypatch, tmp_path)
    assert argv[0] == "/usr/bin/bwrap"
    for flag in ("--unshare-all", "--die-with-parent", "--new-session", "--clearenv"):
        assert flag in argv
    # The tail is the interpreter, isolated from user site, running the module
    # on the one input.
    tail = argv[argv.index("--") + 1:]
    assert tail == [sys.executable, "-s", "-m", "server.convert", "docx", path]
    # The namespace's own scratch space and nothing else is writable.
    assert argv[argv.index("--tmpfs") + 1] == "/tmp"
    assert argv.count("--tmpfs") == 1
    assert "--proc" in argv and "--dev" in argv
    assert argv[argv.index("--chdir") + 1] == "/tmp"
    setenv = [argv[i + 1:i + 3] for i, a in enumerate(argv) if a == "--setenv"]
    assert ["PYTHONPATH", REPO_ROOT] in setenv
    assert ["HOME", "/tmp"] in setenv


def test_the_fresh_mounts_come_before_every_bind(monkeypatch, tmp_path):
    # bwrap applies arguments in order: a --tmpfs /tmp after the binds hides any
    # bound path under /tmp, and on a Linux VM (2026-11-14) every conversion
    # exited 1 that way, with the checkout and venv under /tmp.
    argv, _ = _bwrap_argv(monkeypatch, tmp_path)
    first_bind = min(i for i, a in enumerate(argv) if a in ("--ro-bind", "--ro-bind-try"))
    for fresh in ("--tmpfs", "--proc", "--dev"):
        assert argv.index(fresh) < first_bind, fresh


def test_the_confined_argv_binds_nothing_writable(monkeypatch, tmp_path):
    argv, _ = _bwrap_argv(monkeypatch, tmp_path)
    # Every bind is read-only: not one of the writable forms appears.
    for writable in ("--bind", "--bind-try", "--dev-bind", "--dev-bind-try"):
        assert writable not in argv
    binds = [argv[i + 1:i + 3] for i, a in enumerate(argv) if a in ("--ro-bind", "--ro-bind-try")]
    assert binds and all(src == dst for src, dst in binds)
    bound = {src for src, _ in binds}
    assert {"/usr", REPO_ROOT, os.path.realpath(sys.prefix)} <= bound


def test_the_confined_argv_binds_only_the_one_input_file_from_the_attachments(monkeypatch, tmp_path):
    argv, path = _bwrap_argv(monkeypatch, tmp_path)
    binds = {argv[i + 1] for i, a in enumerate(argv) if a in ("--ro-bind", "--ro-bind-try")}
    assert path in binds
    # Neither the turn's directory (another file's original) nor the root.
    assert str(tmp_path / "attachments") not in binds
    assert os.path.dirname(path) not in binds


def test_nothing_of_the_servers_is_visible_inside(monkeypatch, tmp_path):
    # The opposite namespace to invoke._confinement_prefix(): that one binds
    # `/` and hides the secrets; this one binds almost nothing, so the data
    # dir, ~/.marlow, the device tokens and the APNs key do not exist.
    argv, _ = _bwrap_argv(monkeypatch, tmp_path)
    forbidden = [
        os.path.expanduser("~/.marlow"),
        str(real_settings.data_dir),
        str(real_settings.log_dir),
    ]
    for arg in argv:
        for secret in forbidden:
            assert not arg.startswith(secret), (arg, secret)
    assert "/" not in {argv[i + 1] for i, a in enumerate(argv) if a in ("--ro-bind", "--bind")}
    assert "--share-net" not in argv


def test_without_bwrap_the_argv_is_the_bare_interpreter_and_it_warns_once(
    monkeypatch, tmp_path, caplog
):
    _settings(monkeypatch, tmp_path, bwrap_enabled=False)
    monkeypatch.setattr(attachments, "_warned_unconfined", False)
    path = str(tmp_path / "0.docx")
    with caplog.at_level(logging.WARNING, logger=attachments.logger.name):
        first = attachments._converter_argv("docx", path)
        attachments._converter_argv("xlsx", path)
    assert first == [sys.executable, "-s", "-m", "server.convert", "docx", path]
    warnings = [r for r in caplog.records if "unconfined" in r.getMessage()]
    assert len(warnings) == 1


# --- running it ----------------------------------------------------------------------


def _fake(monkeypatch, code: str) -> None:
    monkeypatch.setattr(
        attachments, "_converter_argv", lambda kind, path: [sys.executable, "-c", code]
    )


def _docx_at(tmp_path) -> str:
    turn = tmp_path / "attachments" / "t1"
    turn.mkdir(parents=True)
    path = turn / "0.docx"
    path.write_bytes(make_docx())
    return str(path)


def test_the_real_converter_writes_the_text_version_beside_the_original(monkeypatch, tmp_path):
    _settings(monkeypatch, tmp_path, bwrap_enabled=False)
    path = _docx_at(tmp_path)
    md_path = attachments.convert_office("docx", path)
    assert md_path == str(tmp_path / "attachments" / "t1" / "0.md")
    text = open(md_path, encoding="utf-8").read()
    assert "# Garden plan" in text and "| Bed | Crop |" in text


def test_a_fake_converter_runs_with_no_stdin_and_a_scrubbed_environment(monkeypatch, tmp_path):
    _settings(monkeypatch, tmp_path, bwrap_enabled=False)
    monkeypatch.setenv("MARLOW_FAKE_SECRET", "s3cret-value")
    _fake(
        monkeypatch,
        "import os, sys; sys.stdin.read(); print('seen:', os.environ.get('MARLOW_FAKE_SECRET'))",
    )
    path = _docx_at(tmp_path)
    text = open(attachments.convert_office("docx", path), encoding="utf-8").read()
    assert text.strip() == "seen: None"


def test_a_converter_that_runs_too_long_is_killed_with_its_children(monkeypatch, tmp_path):
    _settings(monkeypatch, tmp_path, bwrap_enabled=False)
    monkeypatch.setattr(attachments, "CONVERT_TIMEOUT_SECONDS", 1.0)
    pids = tmp_path / "pids"
    _fake(
        monkeypatch,
        "import os, subprocess, sys, time\n"
        "child = subprocess.Popen(['sleep', '60'])\n"
        f"open({str(pids)!r}, 'w').write(f'{{os.getpid()}} {{child.pid}}')\n"
        "time.sleep(60)",
    )
    path = _docx_at(tmp_path)
    started = time.monotonic()
    with pytest.raises(attachments.ConversionError, match="timed out"):
        attachments.convert_office("docx", path)
    assert time.monotonic() - started < 20
    for pid in map(int, pids.read_text().split()):
        _eventually_gone(pid)
    assert not os.path.exists(path.replace(".docx", ".md"))


def _eventually_gone(pid: int) -> None:
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError(f"process {pid} outlived the timeout")


@pytest.mark.parametrize(
    "code, why",
    [
        ("import sys; sys.exit(3)", "exit status"),
        ("pass", "no output"),
        ("import sys; sys.stdout.write('   \\n')", "no output"),
        ("import sys; sys.stdout.buffer.write(b'\\xff\\xfe bad')", "not UTF-8"),
        (
            "import sys; sys.stdout.write('x' * (3 * 1024 * 1024))",
            "output over the limit",
        ),
        ("import os; os.kill(os.getpid(), 11)", "exit status"),
    ],
)
def test_a_failed_conversion_raises_and_writes_nothing(monkeypatch, tmp_path, code, why):
    _settings(monkeypatch, tmp_path, bwrap_enabled=False)
    _fake(monkeypatch, code)
    path = _docx_at(tmp_path)
    with pytest.raises(attachments.ConversionError, match=why):
        attachments.convert_office("docx", path)
    assert not os.path.exists(path.replace(".docx", ".md"))


def test_the_failure_log_carries_no_document_text(monkeypatch, tmp_path, caplog):
    _settings(monkeypatch, tmp_path, bwrap_enabled=False)
    _fake(monkeypatch, "import sys; sys.stderr.write('ClassName: secret document words'); sys.exit(1)")
    path = _docx_at(tmp_path)
    with caplog.at_level(logging.DEBUG, logger=attachments.logger.name):
        with pytest.raises(attachments.ConversionError):
            attachments.convert_office("docx", path)
    assert "secret document words" not in caplog.text


def test_the_archive_check_runs_on_bytes_before_anything_is_written():
    from tests.office_fixtures import zip_bytes

    bomb = zip_bytes({
        "[Content_Types].xml": "<Types/>",
        "word/document.xml": b"\0" * (201 * 1024 * 1024),
    })
    assert len(bomb) < 1024 * 1024  # a megabyte or so of compressed zeros
    assert attachments.archive_refusal(bomb) is not None
    assert attachments.archive_refusal(make_docx()) is None
