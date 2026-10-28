"""Give an existing box the nightly `ideas` job that new installs already get.

The same shape as 0001-email-flash, and for the same reasons (its docstring
and docs/tasks/box-upgrades.md): the schedule store is the agent's, so the
entry is added once, here, and a user who deletes it keeps it deleted. The
prompt, cron/ideas.md, is the software's and update.sh installs it on every
update before this runs.

Ships on: the job writes ideas/ideas.json, which the app shows behind the
lightbulb (docs/tasks/ideas-panel.md). Without the job the panel is empty on
every box but the one it was trialled on. An idea is an offer the user taps or
ignores, so there is nothing to consent to before it runs.

Timed from the box's own `consolidate` entry, half an hour before it, because
the job reads the day's conversation and consolidate is what compacts it away.
Only a plain every-day time is understood. A consolidate that is a list, a
range, a step or a weekday set has no single "before" to aim at, so that box
is skipped and the agent can add the job when asked.

Deferred, not skipped, on a box without PREFERENCES.md. That file marks the
split layout, and only there does update.sh install the CLAUDE.md whose Ideas
section tells the chat side what an `[Idea: …]` line means and that the
responses file is the server's. A job its own identity knows nothing about gets
routed around. Exit 75 retries every deploy, so the job lands on the first one
after the box is converted (deploy/convert-identity.md).

Idempotent: an existing `ideas` entry (by id OR by job) is left alone. Mike's
box is that case; its entry predates this file.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _store import EXIT_DEFERRED, EXIT_DONE, EXIT_FAILED, load, rejection, write  # noqa: E402

JOB = "ideas"
LEAD_MINUTES = 30

_INT_RE = re.compile(r"[0-9]+\Z")


def _ideas_cron(consolidate_cron: str) -> tuple[str | None, str]:
    """(cron for ideas or None, a reason to print)."""
    fields = consolidate_cron.split()
    simple = (
        len(fields) == 5
        and _INT_RE.fullmatch(fields[0])
        and _INT_RE.fullmatch(fields[1])
        and fields[2:] == ["*", "*", "*"]
    )
    if not simple:
        return None, f"consolidate cron {consolidate_cron!r} is not a plain daily time"
    # Every day, so wrapping past midnight lands on the evening before, which is
    # still "before consolidate".
    total = (int(fields[1]) * 60 + int(fields[0]) - LEAD_MINUTES) % (24 * 60)
    return f"{total % 60} {total // 60} * * *", ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--working-dir", required=True, type=Path)
    # Part of the runner's interface; unused, as in 0001.
    parser.add_argument("--repo", required=True, type=Path)
    args = parser.parse_args(argv)

    cron_dir = args.working_dir / "cron"
    store_path = cron_dir / "schedules.json"
    prompt_path = cron_dir / f"{JOB}.md"

    store = load(store_path)
    if store is None:
        return EXIT_DEFERRED
    entries = store["entries"]

    if not (args.working_dir / "PREFERENCES.md").is_file():
        print("no PREFERENCES.md: this box predates the split; will retry next deploy")
        return EXIT_DEFERRED

    if any(e.get("id") == JOB or e.get("job") == JOB for e in entries):
        print("already scheduled")
        return EXIT_DONE

    consolidate_index = next(
        (i for i, e in enumerate(entries) if e.get("id") == "consolidate" and "cron" in e),
        None,
    )
    if consolidate_index is None:
        print("no consolidate entry to anchor to")
        return EXIT_DONE
    cron, why = _ideas_cron(entries[consolidate_index]["cron"])
    if cron is None:
        print(why)
        return EXIT_DONE

    if not prompt_path.is_file():
        print(f"{prompt_path} is missing; update.sh should have installed it")
        return EXIT_FAILED

    entries.insert(consolidate_index, {"id": JOB, "cron": cron, "job": JOB})

    why = rejection(store, store_path)
    if why is not None:
        print(f"not added: the store would be rejected ({why})")
        return EXIT_DONE

    write(store, store_path)
    print(f"added {JOB} at {cron}")
    return EXIT_DONE


if __name__ == "__main__":
    sys.exit(main())
