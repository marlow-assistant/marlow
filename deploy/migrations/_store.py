"""Shared by the migrations that change cron/schedules.json.

Not a migration itself: the runner in update.sh only runs files named
NNNN-*.py, so this one is never run on its own.

The exit codes are the runner's whole protocol (docs/tasks/box-upgrades.md):
  0   done: applied, or nothing to do, or deliberately skipped. Recorded, so
      never retried. Skips use this too: a skip's reason does not change by
      waiting, and re-printing it on every deploy would be noise.
  75  deferred (EX_TEMPFAIL): the store is missing or currently invalid, which
      is the agent's to fix. Not recorded, so the next deploy tries again.
  any other: a real failure, and the runner aborts the deploy.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import schedules  # noqa: E402

EXIT_DONE = 0
EXIT_DEFERRED = 75
EXIT_FAILED = 1


def load(store_path: Path) -> dict | None:
    """The parsed store, or None (after printing why) if it should wait."""
    if not store_path.is_file():
        print(f"no {store_path}; will retry next deploy")
        return None
    # rejection_reason runs the real validator and also swallows unreadable
    # JSON into a message. An invalid store is the agent's to repair; stacking
    # a migration onto it would only bury which of the two edits broke it.
    reason = schedules.rejection_reason(store_path)
    if reason is not None:
        print(f"schedules.json is invalid ({reason}); will retry next deploy")
        return None
    return json.loads(store_path.read_text())


def rejection(store: dict, store_path: Path) -> str | None:
    """Why the server would refuse this store, or None if it would load it.

    Checked BEFORE the write. The daily firing cap and the entry cap are the
    likeliest ways for a busy store to refuse a migration's change, and writing
    a store the server then rejects freezes the crontab for every entry, not
    just the new one (see server/schedules.py). The validator also checks that
    every `job` entry has its prompt file.
    """
    try:
        schedules._parse_store(store, store_path)
    except schedules.ScheduleValidationError as exc:
        return str(exc)
    return None


def write(store: dict, store_path: Path) -> None:
    # Same-directory temp + os.replace: the server and the agent both read this
    # file at any moment, and a half-written store is a rejected store.
    fd, temp_name = tempfile.mkstemp(dir=store_path.parent, prefix=".schedules.json.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(store, indent=2) + "\n")
        # mkstemp makes the file 0600; keep the store's own mode so the agent
        # (same user) and anything reading it see what they did before.
        shutil.copymode(store_path, temp_name)
        os.replace(temp_name, store_path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
