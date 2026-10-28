"""scripts/backup-identity-bundle-scheduled.sh: notify only when the newest bundle is stale."""
from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backup-identity-bundle-scheduled.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")


def _stub(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)
    return path


def _bundle(d: Path, days_ago: int) -> None:
    stamp = (dt.date.today() - dt.timedelta(days=days_ago)).isoformat()
    (d / f"marlow-identity-{stamp}.bundle").write_bytes(b"x")


def _run(tmp_path: Path, backup_exit: int = 0):
    bundles = tmp_path / "bundles"
    bundles.mkdir(exist_ok=True)
    notes = tmp_path / "notes"
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "MARLOW_BACKUP_SCRIPT": str(_stub(tmp_path / "backup.sh", f"echo ran\nexit {backup_exit}\n")),
        "MARLOW_BACKUP_NOTIFY": str(_stub(tmp_path / "notify.sh", f'echo "$1" >> "{notes}"\n')),
        "MARLOW_BUNDLE_DIR": str(bundles),
        "MARLOW_BACKUP_LOG": str(tmp_path / "backup.log"),
    }
    r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env)
    note = notes.read_text() if notes.exists() else ""
    return r, note, (tmp_path / "backup.log").read_text()


def test_fresh_bundle_is_quiet(tmp_path: Path) -> None:
    (tmp_path / "bundles").mkdir()
    _bundle(tmp_path / "bundles", 0)
    r, note, log = _run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert note == ""
    assert "backup ok" in log and "fresh" in log


def test_one_failed_run_with_a_recent_bundle_does_not_notify(tmp_path: Path) -> None:
    # Off the tailnet for a day is delay, not loss.
    (tmp_path / "bundles").mkdir()
    _bundle(tmp_path / "bundles", 2)
    r, note, log = _run(tmp_path, backup_exit=1)
    assert r.returncode == 0
    assert note == ""
    assert "backup FAILED" in log


def test_stale_newest_bundle_notifies_with_its_age(tmp_path: Path) -> None:
    (tmp_path / "bundles").mkdir()
    _bundle(tmp_path / "bundles", 9)
    _bundle(tmp_path / "bundles", 4)
    r, note, log = _run(tmp_path, backup_exit=1)
    assert r.returncode == 1
    assert "4 days old" in note
    assert "STALE" in log


def test_no_bundle_at_all_notifies(tmp_path: Path) -> None:
    r, note, _ = _run(tmp_path, backup_exit=1)
    assert r.returncode == 1
    assert "No identity backup" in note


def test_partial_and_stray_files_do_not_count_as_fresh(tmp_path: Path) -> None:
    bundles = tmp_path / "bundles"
    bundles.mkdir()
    _bundle(bundles, 5)
    today = dt.date.today().isoformat()
    (bundles / f"marlow-identity-{today}.bundle.partial").write_bytes(b"x")
    (bundles / "marlow-identity-copy.bundle").write_bytes(b"x")
    r, note, _ = _run(tmp_path, backup_exit=1)
    assert r.returncode == 1
    assert "5 days old" in note


def test_unreadable_date_in_newest_name_notifies_instead_of_passing(tmp_path: Path) -> None:
    bundles = tmp_path / "bundles"
    bundles.mkdir()
    _bundle(bundles, 0)
    (bundles / "marlow-identity-copy-of-me.bundle").write_bytes(b"x")  # matches ????-??-??, sorts last
    r, note, log = _run(tmp_path)
    assert r.returncode == 1
    assert "can't read" in note
    assert "fresh" not in log
