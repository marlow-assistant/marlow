"""Hand the harness's jobs to the harness: seed "Your day", drop their entries.

docs/tasks/default-infrastructure.md, steps 2 and 3, which ship as one
release. `ideas`, `consolidate` and the check-in (`email-flash`) stop being
cron/schedules.json entries; the renderer adds them itself, timed from "Your
day" and the check-in interval in the server's preferences. One migration, not
one per step: the validator refuses all three, so a migration that removed
only some would find the store invalid and defer forever. Both halves are
here, in this order, because the first reads what the second removes:

1. **Seed the day so nothing moves on upgrade.** The end is the box's
   `consolidate` time minus two hours and the start is its `morning` time, each
   only when it is a plain every-day time; otherwise the default (23:00,
   08:00). A day equal to the defaults, or one that fails validation, is not
   written: null already means the defaults, and a half-valid day is worse
   than none. A day already set (a rerun, or the user got to Settings first)
   is left alone. Seeded even when step 2 has to wait (see `main`).

   The check-in interval is the step between the `email-flash` hours when they
   are evenly spaced every day, else the default (3). With no `email-flash`
   entry it is 0 (off), because the user deleted it and that choice holds --
   but only on a box that has been paired. A fresh install runs every
   migration before anyone pairs, and its seed has no entry either; turning
   check-ins off there would be wrong.
2. **Remove every entry whose `job` is the harness's,** printing each one's
   old cron in case someone had moved it on purpose.

Not `_store.load`: the validator now refuses exactly these entries, so every
existing box would read as invalid and defer forever. The store is checked
with the entries removed instead; anything else wrong with it is the agent's
to fix, so that defers (75) and touches nothing.

The preferences live in data_dir, which bwrap hides from the agent but not
from this script: it runs as the box's user (update.sh), like the server.
update.sh runs migrations while the old server is still up; the old server
keeps keys it doesn't know when it rewrites that file, so the seed survives,
and the new server re-renders the crontab when it starts.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _store import EXIT_DEFERRED, EXIT_DONE, rejection, write  # noqa: E402
from server.config import settings  # noqa: E402
from server.preferences import (  # noqa: E402
    DEFAULT_CHECKIN_EVERY_HOURS,
    DEFAULT_DAY_END,
    DEFAULT_DAY_START,
    MAX_CHECKIN_EVERY_HOURS,
    DayError,
    PreferencesStore,
    validate_day,
)
from server.schedules import CHECKIN_JOB, HARNESS_JOBS, checkin_crons  # noqa: E402

_INT_RE = re.compile(r"[0-9]+\Z")
CONSOLIDATE_AFTER_END_MINUTES = 120


def _daily_minutes(cron: str) -> int | None:
    """Minutes after midnight for a plain every-day cron, else None."""
    fields = cron.split() if isinstance(cron, str) else []
    if len(fields) == 5 and _INT_RE.fullmatch(fields[0]) and _INT_RE.fullmatch(fields[1]) and fields[2:] == ["*", "*", "*"]:
        minute, hour = int(fields[0]), int(fields[1])
        if minute < 60 and hour < 24:
            return hour * 60 + minute
    return None


def _hhmm(total: int) -> str:
    total %= 24 * 60
    return f"{total // 60:02d}:{total % 60:02d}"


def _derived_day(entries: list[dict]) -> tuple[str, str]:
    start, end = DEFAULT_DAY_START, DEFAULT_DAY_END
    for entry in entries:
        at = _daily_minutes(entry.get("cron"))
        if at is None:
            continue
        if entry.get("job") == "consolidate":
            end = _hhmm(at - CONSOLIDATE_AFTER_END_MINUTES)
        elif entry.get("id") == "morning":
            start = _hhmm(at)
    return start, end


def _seed_day(entries: list[dict], store: PreferencesStore) -> None:
    start, end = _derived_day(entries)
    if (start, end) == (DEFAULT_DAY_START, DEFAULT_DAY_END):
        print("day: the box ran on the default times; nothing to seed")
        return
    if store.day_is_set():
        print("day: already set; left alone")
        return
    try:
        validate_day(start, end)
    except DayError as e:
        print(f"day: {start}-{end} from this box's schedule is not a valid day ({e.code}); left at the defaults")
        return
    store.update(day_start=start, day_end=end)
    print(f"day: seeded {start}-{end} from this box's schedule")


def _checkin_step(entries: list[dict], paired: bool) -> tuple[int, str]:
    """(interval, why) from the box's `email-flash` entries."""
    flashes = [e for e in entries if e.get("job") == CHECKIN_JOB]
    if not flashes:
        if paired:
            return 0, "no check-in entry on a paired box: the user turned it off"
        return DEFAULT_CHECKIN_EVERY_HOURS, "no check-in entry, but never paired: a fresh install"
    cron = flashes[0].get("cron") if len(flashes) == 1 else None
    fields = cron.split() if isinstance(cron, str) else []
    if len(fields) == 5 and _INT_RE.fullmatch(fields[0]) and fields[2:] == ["*", "*", "*"]:
        hours = fields[1].split(",")
        if len(hours) >= 2 and all(_INT_RE.fullmatch(h) for h in hours):
            steps = {int(b) - int(a) for a, b in zip(hours, hours[1:])}
            if len(steps) == 1 and 1 <= (step := steps.pop()) <= MAX_CHECKIN_EVERY_HOURS:
                return step, f"every {step}h, as the box ran it"
    return DEFAULT_CHECKIN_EVERY_HOURS, "the box's check-in times aren't an even daily step"


def _seed_checkin(entries: list[dict], store: PreferencesStore, paired: bool) -> None:
    if store.checkin_is_set():
        print("check-in: already set; left alone")
        return
    step, why = _checkin_step(entries, paired)
    if step == DEFAULT_CHECKIN_EVERY_HOURS:
        print(f"check-in: the default ({why}); nothing to seed")
        return
    store.update(checkin_every_hours=step)
    print(f"check-in: seeded {'off' if step == 0 else f'every {step}h'} ({why})")


def _report_checkin_move(entries: list[dict], store: PreferencesStore) -> None:
    """Say so when the check-in will fire at different times than before.

    The harness renders one shape, start + N... every day, so a check-in the
    user had moved on its own (not anchored to `morning`, weekdays only, a
    one-off) can't be kept exactly. Nobody reads a schedule diff, so the
    deploy output names the change; the user can retune it in Settings.
    """
    # str(): these entries were never validated (see main), and a non-string
    # field must not crash the deploy over a report line.
    before = sorted(str(e.get("cron") or e.get("at") or "?") for e in entries if e.get("job") == CHECKIN_JOB)
    start, end = store.day()
    after = [cron for _, cron in checkin_crons(start, end, store.checkin_every_hours())]
    if before != after:
        print(f"check-in: times change from {before or 'none'} to {after or 'none (off)'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--working-dir", required=True, type=Path)
    # Part of the runner's interface; unused, as in 0001.
    parser.add_argument("--repo", required=True, type=Path)
    # Not passed by update.sh: the default is the box user's own data_dir,
    # since the runner uses `sudo -H`. For tests; the device tokens are read
    # beside it.
    parser.add_argument("--preferences", type=Path, default=None)
    args = parser.parse_args(argv)
    tokens = (
        args.preferences.parent / settings.device_tokens_path.name
        if args.preferences is not None
        else settings.device_tokens_path
    )

    store_path = args.working_dir / "cron" / "schedules.json"
    if not store_path.is_file():
        print(f"no {store_path}; will retry next deploy")
        return EXIT_DEFERRED
    try:
        store = json.loads(store_path.read_text())
        entries = store["entries"]
        if not isinstance(entries, list):
            raise TypeError("entries is not a list")
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"schedules.json is unreadable ({e}); will retry next deploy")
        return EXIT_DEFERRED

    plumbing = [e for e in entries if isinstance(e, dict) and e.get("job") in HARNESS_JOBS]
    if not plumbing:
        print("no ideas, consolidate or check-in entries; nothing to hand over")
        return EXIT_DONE

    # Seeded BEFORE the validity check below, and even when that defers. A
    # deferred store is the agent's to fix, and the validator's message tells
    # it to delete these very entries; once it has, a later run finds nothing
    # to hand over, and a day seeded only on success would be lost with them.
    preferences = PreferencesStore(args.preferences)
    _seed_day(entries, preferences)
    _seed_checkin(entries, preferences, paired=tokens.exists())
    _report_checkin_move(entries, preferences)

    remaining = {**store, "entries": [e for e in entries if e not in plumbing]}
    why = rejection(remaining, store_path)
    if why is not None:
        print(f"schedules.json is invalid without them too ({why}); will retry next deploy")
        return EXIT_DEFERRED

    write(remaining, store_path)
    for entry in plumbing:
        when = entry.get("cron") or entry.get("at")
        # !r throughout: agent-written and unvalidated (the validator only
        # sees what remains), so no raw control characters reach the
        # terminal running the deploy.
        print(f"removed {entry.get('id')!r} (job {entry.get('job')!r}, was {when!r}); the server runs it now")
    return EXIT_DONE


if __name__ == "__main__":
    sys.exit(main())
