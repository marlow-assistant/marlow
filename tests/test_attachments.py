"""server/attachments.py — storage for uploaded images
(docs/tasks/ios-image-input.md)."""
from __future__ import annotations

import dataclasses
import os
import time

from server import attachments
from server.config import settings as real_settings


def _with_attachments_dir(monkeypatch, path) -> None:
    monkeypatch.setattr(
        attachments, "settings", dataclasses.replace(real_settings, attachments_dir=path)
    )


def test_write_attachment_creates_the_turn_dir_and_file(tmp_path, monkeypatch) -> None:
    _with_attachments_dir(monkeypatch, tmp_path)
    paths = attachments.write_attachments("turn-1", [b"fake jpeg bytes"])
    assert paths == (str(tmp_path / "turn-1" / "0.jpg"),)
    with open(paths[0], "rb") as f:
        assert f.read() == b"fake jpeg bytes"
    assert ((tmp_path / "turn-1").stat().st_mode & 0o777) == 0o700


def test_write_attachments_numbers_files_in_order_in_one_turn_dir(tmp_path, monkeypatch) -> None:
    _with_attachments_dir(monkeypatch, tmp_path)
    paths = attachments.write_attachments("turn-1", [b"a", b"b", b"c"])
    assert paths == tuple(str(tmp_path / "turn-1" / f"{i}.jpg") for i in range(3))
    assert [open(p, "rb").read() for p in paths] == [b"a", b"b", b"c"]
    attachments.cleanup_attachment("turn-1")
    assert not (tmp_path / "turn-1").exists()


def test_cleanup_attachment_removes_the_turn_dir(tmp_path, monkeypatch) -> None:
    _with_attachments_dir(monkeypatch, tmp_path)
    attachments.write_attachments("turn-1", [b"x"])
    attachments.cleanup_attachment("turn-1")
    assert not (tmp_path / "turn-1").exists()


def test_cleanup_attachment_is_a_noop_when_nothing_is_there(tmp_path, monkeypatch) -> None:
    # An ERRORed turn that never got as far as writing anything must not raise
    # when the drain loop unconditionally calls cleanup on it.
    _with_attachments_dir(monkeypatch, tmp_path)
    attachments.cleanup_attachment("never-existed")


def test_sweep_removes_dirs_older_than_24h(tmp_path, monkeypatch) -> None:
    _with_attachments_dir(monkeypatch, tmp_path)
    old = tmp_path / "old-turn"
    old.mkdir()
    (old / "0.jpg").write_bytes(b"x")
    now = time.time()
    old_time = now - 25 * 3600
    os.utime(old, (old_time, old_time))

    removed = attachments.sweep_orphaned_attachments(now=now)
    assert removed == 1
    assert not old.exists()


def test_sweep_leaves_recent_dirs_alone(tmp_path, monkeypatch) -> None:
    _with_attachments_dir(monkeypatch, tmp_path)
    recent = tmp_path / "recent-turn"
    recent.mkdir()

    removed = attachments.sweep_orphaned_attachments(now=time.time())
    assert removed == 0
    assert recent.exists()


def test_sweep_on_a_missing_root_is_a_noop(tmp_path, monkeypatch) -> None:
    missing = tmp_path / "does-not-exist"
    _with_attachments_dir(monkeypatch, missing)
    assert attachments.sweep_orphaned_attachments() == 0
