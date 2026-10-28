"""Give an existing box the `email-flash` job that new installs already get.

New installs seed `email-flash` from identity/cron/schedules.json. A box that
was installed before it existed has a store the AGENT owns, so update.sh cannot
overwrite it without erasing the agent's own schedules; the entry has to be
added to the live store instead. That is a data migration, and it follows the
two policies in docs/tasks/box-upgrades.md:

- Ships on (section 2). `email-flash` is a feature, not an offer: a box without
  Gmail replies `[silent]` and the server drops it, so there is no consent to
  ask for and nothing to build an opt-in around.
- Runs once (section 3). The runner records a 0 exit and never runs this again,
  which is the only reason a user can delete the entry and have it stay
  deleted. A plain "ensure it is there" on every deploy would put it back.

Only the schedule entry. The prompt, cron/email-flash.md, is the software's:
update.sh installs it on every update, before this runs (box-upgrades.md
"Still open" #2). Whether the job runs is the user's, and that is what this
adds, once.

Idempotent anyway, because the record file can be lost and Mike may already
have added the entry by hand: an existing `email-flash` (by id OR by job) is
left alone rather than duplicated.

Exit codes: deploy/migrations/_store.py. A missing prompt is a real failure
(EXIT_FAILED): update.sh installs it before this runs, so its absence means the
deploy itself is broken.

The time is derived from the box's own `morning` entry (three-hourly after it,
up to 17:00) rather than hard-coded to the seed's 11/14/17, because a
user who moved their morning brief to 06:30 would otherwise get a flash before
their brief. Only a plain daily time is understood: for a list, range or step
there are several defensible anchors, and a wrong guess puts turns on a
schedule the user never chose. Not guessing is cheaper than being wrong, so
those boxes are skipped and the agent can still add the job when asked.

The finished store goes through the real validator before it is written
(_store.rejection).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _store import EXIT_DEFERRED, EXIT_DONE, EXIT_FAILED, load, rejection, write  # noqa: E402

JOB = "email-flash"
# Three hours apart, and stop at 17:00. The reason given then was that the
# reply goes straight to the phone, so an evening check is an interruption.
# Reversed 2026-10-08: two bank alerts arrived in the evening, after the last
# flash, and waited for the next morning's briefing, while another assistant
# flagged them that evening.
# 0002-email-flash-evening adds the evening run. This file has already run on
# Mike's box, so it is left as it was and 0002 extends what it produced.
STEP_HOURS = 3
LAST_HOUR = 17

_INT_RE = re.compile(r"[0-9]+\Z")


def _flash_cron(morning_cron: str) -> tuple[str | None, str]:
    """(cron for email-flash or None, a reason to print)."""
    fields = morning_cron.split()
    simple = (
        len(fields) == 5
        and _INT_RE.fullmatch(fields[0])
        and _INT_RE.fullmatch(fields[1])
        and fields[2] == "*"
        and fields[3] == "*"
    )
    if not simple:
        return None, f"morning cron {morning_cron!r} is not a simple daily time"
    minute, hour, weekday = int(fields[0]), int(fields[1]), fields[4]
    # Every firing at or before 17:00: a morning at 8:30 gets 11:30, 14:30 and
    # no 17:30, which would be after the cutoff.
    last = LAST_HOUR if minute == 0 else LAST_HOUR - 1
    hours = list(range(hour + STEP_HOURS, last + 1, STEP_HOURS))
    if not hours:
        return None, f"morning at {hour}:00 is too late in the day for an inbox check"
    # The weekday field is copied through verbatim: weekdays-only mornings get
    # weekdays-only flashes, which is what "after the morning brief" means.
    return f"{minute} {','.join(str(h) for h in hours)} * * {weekday}", ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--working-dir", required=True, type=Path)
    # Part of the runner's interface; unused here since the prompt moved to
    # update.sh, kept so every migration takes the same arguments.
    parser.add_argument("--repo", required=True, type=Path)
    args = parser.parse_args(argv)

    cron_dir = args.working_dir / "cron"
    store_path = cron_dir / "schedules.json"
    prompt_path = cron_dir / f"{JOB}.md"

    store = load(store_path)
    if store is None:
        return EXIT_DEFERRED
    entries = store["entries"]

    if any(e.get("id") == JOB or e.get("job") == JOB for e in entries):
        print("already scheduled")
        return EXIT_DONE

    morning_index = next(
        (i for i, e in enumerate(entries) if e.get("id") == "morning" and "cron" in e),
        None,
    )
    if morning_index is None:
        print("no morning entry to anchor to")
        return EXIT_DONE
    cron, why = _flash_cron(entries[morning_index]["cron"])
    if cron is None:
        print(why)
        return EXIT_DONE

    if not prompt_path.is_file():
        print(f"{prompt_path} is missing; update.sh should have installed it")
        return EXIT_FAILED

    entries.insert(morning_index + 1, {"id": JOB, "cron": cron, "job": JOB})

    why = rejection(store, store_path)
    if why is not None:
        print(f"not added: the store would be rejected ({why})")
        return EXIT_DONE

    write(store, store_path)
    print(f"added {JOB} at {cron}")
    return EXIT_DONE


if __name__ == "__main__":
    sys.exit(main())
