"""Move two hand-made jobs' `high` effort from server code onto their entries.

Until per-entry effort (docs/tasks/model-and-effort-config.md "Per-job
effort"), `app.HIGH_EFFORT_JOBS` named Mike's own `weekly-digest` and
`alberta-weekly` alongside the product jobs. Product code should not name one
user's jobs, so the names left the code set and the effort moved to the
schedules entry that runs each job. Without this, both would silently drop to
`cron_effort` (medium) on upgrade.

Only an entry whose `job` is one of the two is touched, and only if it has no
effort of its own: one already set was chosen since, and is left as it is. A
box with neither job (every box but Mike's) is unchanged, which is also what
keeps this a no-op on a fresh install.

The running server is the OLD one while migrations run (update.sh restarts
after them), and its validator refuses the new key. Harmless: a store it
rejects leaves the previous crontab in place, and nothing renders from the
store until the restart a minute later.

Not deferred on a box without PREFERENCES.md, like 0005: nothing in CLAUDE.md
needs to know about an entry's effort for it to work.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _store import EXIT_DEFERRED, EXIT_DONE, load, rejection, write  # noqa: E402

JOBS = ("weekly-digest", "alberta-weekly")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--working-dir", required=True, type=Path)
    parser.add_argument("--repo", required=True, type=Path)
    args = parser.parse_args(argv)

    store_path = args.working_dir / "cron" / "schedules.json"
    store = load(store_path)
    if store is None:
        return EXIT_DEFERRED

    changed = [
        entry for entry in store["entries"]
        if entry.get("job") in JOBS and "effort" not in entry
    ]
    if not changed:
        print("nothing to move")
        return EXIT_DONE
    for entry in changed:
        entry["effort"] = "high"

    why = rejection(store, store_path)
    if why is not None:
        print(f"not changed: the store would be rejected ({why})")
        return EXIT_DONE

    write(store, store_path)
    print("set effort high on " + ", ".join(entry["id"] for entry in changed))
    return EXIT_DONE


if __name__ == "__main__":
    sys.exit(main())
