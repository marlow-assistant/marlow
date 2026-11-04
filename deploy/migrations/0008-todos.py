"""Move a box's to-dos into the to-do store: schedule the one-off job that does it.

docs/tasks/todos-and-reminders.md, part 2 ("Migration"). The store is new, and
until a box's to-dos are in it the old system keeps being used: a rule in a
memory file routes "add to my list" to a file, a briefing prompt reads that
file, a one-off `notify` stands in for the item's reminder. Each box keeps its
to-dos differently (the task doc's inventory), so no script can tell an open
to-do from a plan, a routine or a note. The box's own Marlow can, so this
schedules a turn for it: cron/todo-migration.md, which update.sh installs as a
shipped prompt before the migrations run. That turn moves the items, rewrites
the rules and prompts that point elsewhere, and writes a report on the box
listing every to-do-like line it found and what became of it.

A one-off `at` entry, ten minutes out, pinned to the box's offset. update.sh
restarts the server right after the migrations, so the new server, with the
to-do tools, is the one that runs it. The store prunes a fired one-off itself.
At `high` effort: it is one careful read of the whole identity tree, nobody is
waiting on it, and it writes what every later turn works from.

Only on a box that has been paired. A fresh install runs every migration
before anyone pairs and has nothing to move; it starts on the store, so this
is a no-op there, as the fresh-seed invariant requires.

Deferred (75) on a box without PREFERENCES.md: the job leans on the identity's
To-dos section, which update.sh installs only on the split layout (the same
reason as 0003). Also deferred, not skipped, when the store is invalid or
would be refused with the entry added (the entry cap, the daily firings): the
move matters too much to drop for good, and the next deploy tries again.

Idempotent: an entry with this id or job already there is left alone.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _store import EXIT_DEFERRED, EXIT_DONE, EXIT_FAILED, load, rejection, write  # noqa: E402
from server.config import settings  # noqa: E402
# The harness's own pinned form, so the next sync leaves the value as it is.
from server.schedules import _AT_PINNED_FORMAT  # noqa: E402

JOB = "todo-migration"
LEAD = timedelta(minutes=10)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--working-dir", required=True, type=Path)
    # Part of the runner's interface; unused, as in 0001.
    parser.add_argument("--repo", required=True, type=Path)
    # Not passed by update.sh: the default is the box user's own data_dir
    # (the runner uses `sudo -H`). For tests.
    parser.add_argument("--data-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    data_dir = args.data_dir if args.data_dir is not None else settings.data_dir

    if not (data_dir / settings.device_tokens_path.name).exists():
        print("never paired: a fresh install has no to-dos to move")
        return EXIT_DONE

    if not (args.working_dir / "PREFERENCES.md").is_file():
        print("no PREFERENCES.md: this box predates the split; will retry next deploy")
        return EXIT_DEFERRED

    store_path = args.working_dir / "cron" / "schedules.json"
    store = load(store_path)
    if store is None:
        return EXIT_DEFERRED
    entries = store["entries"]

    if any(e.get("id") == JOB or e.get("job") == JOB for e in entries):
        print("already scheduled")
        return EXIT_DONE

    if not (args.working_dir / "cron" / f"{JOB}.md").is_file():
        print(f"cron/{JOB}.md is missing; update.sh should have installed it")
        return EXIT_FAILED

    at = (datetime.now().astimezone() + LEAD).strftime(_AT_PINNED_FORMAT)
    entries.append({"id": JOB, "at": at, "job": JOB, "effort": "high"})

    why = rejection(store, store_path)
    if why is not None:
        print(f"not added: the store would be rejected ({why}); will retry next deploy")
        return EXIT_DEFERRED

    write(store, store_path)
    print(f"scheduled {JOB} at {at}: this box's Marlow moves its to-dos into the store")
    return EXIT_DONE


if __name__ == "__main__":
    sys.exit(main())
