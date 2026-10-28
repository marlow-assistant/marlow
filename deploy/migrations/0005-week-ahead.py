"""Give an existing box the Sunday-evening `week-ahead` job that new installs get.

Added with the goal-led first run (docs/tasks/friends-alpha.md item 9): an
assistant that keeps someone organized should look at the coming week before
it starts, not only at today. Mike chose it on by default (2026-10-17).

cron/week-ahead.md is a STARTER, like morning.md: the user's to change after
it lands, so update.sh never installs it (SHIPPED_JOBS) and this copies it in
only when the box has none. An existing file is theirs and is left as it is.

Skipped for good on a box that already runs something on Sunday. Mike's box
has a hand-made Sunday-evening digest (`weekly-digest`, `0 18 * * 0`), and a
second Sunday summary beside it is noise. Only a plain Sunday day-of-week is
recognised (0 or 7, alone or in a list; the store's validator refuses day
names); a range is not worth parsing, and the agent can add the job when asked.

Not deferred on a box without PREFERENCES.md, unlike 0003: nothing in
CLAUDE.md needs to know about this job for it to work.

Idempotent: an existing `week-ahead` entry (by id OR by job) is left alone,
which is also what makes this a no-op on a fresh install.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _store import EXIT_DEFERRED, EXIT_DONE, EXIT_FAILED, load, rejection, write  # noqa: E402

JOB = "week-ahead"
CRON = "0 18 * * 0"

_SUNDAY = {"0", "7"}


def _runs_on_sunday(entry: dict) -> bool:
    fields = entry.get("cron", "").split()
    return len(fields) == 5 and any(tok in _SUNDAY for tok in fields[4].split(","))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--working-dir", required=True, type=Path)
    parser.add_argument("--repo", required=True, type=Path)
    args = parser.parse_args(argv)

    cron_dir = args.working_dir / "cron"
    store_path = cron_dir / "schedules.json"
    prompt_path = cron_dir / f"{JOB}.md"
    template = args.repo / "identity" / "cron" / f"{JOB}.md"

    store = load(store_path)
    if store is None:
        return EXIT_DEFERRED
    entries = store["entries"]

    if any(e.get("id") == JOB or e.get("job") == JOB for e in entries):
        print("already scheduled")
        return EXIT_DONE

    sunday = next((e["id"] for e in entries if _runs_on_sunday(e)), None)
    if sunday is not None:
        print(f"already has a Sunday job ({sunday}); not adding a second")
        return EXIT_DONE

    if not template.is_file():
        print(f"{template} is missing from the repo")
        return EXIT_FAILED

    copied = not prompt_path.is_file()
    if copied:
        shutil.copyfile(template, prompt_path)
        shutil.copymode(store_path, prompt_path)

    entries.append({"id": JOB, "cron": CRON, "job": JOB})

    why = rejection(store, store_path)
    if why is not None:
        if copied:
            prompt_path.unlink()
        print(f"not added: the store would be rejected ({why})")
        return EXIT_DONE

    write(store, store_path)
    print(f"added {JOB} at {CRON}")
    return EXIT_DONE


if __name__ == "__main__":
    sys.exit(main())
