"""Give `email-flash` one evening run, three hours after its last.

0001 stopped the inbox check at 17:00, on the theory that an evening message
is an interruption. That was wrong for the mail the check exists for. Mike's
box, 2026-10-08: two bank alerts arrived in the evening, after the last flash,
so Marlow only reported them in the next morning's briefing, while another
assistant flagged them that evening.
New installs get the evening run from identity/cron/schedules.json.

Only the shape 0001 or the seed produced is extended: a single daily minute,
two or more hours exactly three apart, every day of the month. That is
"Marlow's default, untouched". Any other shape means the user or the agent
chose the times, and whether the job runs and when is theirs
(box-upgrades.md "Still open" #2), so it is left alone. A last run already at
18:00 or later is already an evening run. A box with no `email-flash` (never
had it, or the user deleted it) is left without one: this migration extends
the job, it doesn't bring it back.

Exit codes: deploy/migrations/_store.py.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _store import EXIT_DEFERRED, EXIT_DONE, load, rejection, write  # noqa: E402

JOB = "email-flash"
STEP_HOURS = 3
EVENING = 18

_INT_RE = re.compile(r"[0-9]+\Z")


def _evening_cron(cron: str) -> tuple[str | None, str]:
    """(the extended cron or None, a reason to print)."""
    fields = cron.split()
    if len(fields) != 5 or fields[2] != "*" or fields[3] != "*" or not _INT_RE.fullmatch(fields[0]):
        return None, f"{JOB} cron {cron!r} isn't the default shape; left alone"
    parts = fields[1].split(",")
    if len(parts) < 2 or not all(_INT_RE.fullmatch(p) for p in parts):
        return None, f"{JOB} cron {cron!r} isn't the default shape; left alone"
    hours = [int(p) for p in parts]
    if any(b - a != STEP_HOURS for a, b in zip(hours, hours[1:])):
        return None, f"{JOB} hours {fields[1]} aren't three apart; left alone"
    if hours[-1] >= EVENING:
        return None, f"{JOB} already runs in the evening"
    # hours[-1] is at most 17 here, so the new run is at most 20:59.
    hours.append(hours[-1] + STEP_HOURS)
    fields[1] = ",".join(str(h) for h in hours)
    return " ".join(fields), ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--working-dir", required=True, type=Path)
    parser.add_argument("--repo", required=True, type=Path)
    args = parser.parse_args(argv)

    store_path = args.working_dir / "cron" / "schedules.json"
    store = load(store_path)
    if store is None:
        return EXIT_DEFERRED

    entry = next(
        (e for e in store["entries"] if e.get("id") == JOB and e.get("job") == JOB and "cron" in e),
        None,
    )
    if entry is None:
        print(f"no recurring entry with id and job {JOB}; left alone")
        return EXIT_DONE
    cron, why = _evening_cron(entry["cron"])
    if cron is None:
        print(why)
        return EXIT_DONE

    entry["cron"] = cron
    why = rejection(store, store_path)
    if why is not None:
        print(f"not extended: the store would be rejected ({why})")
        return EXIT_DONE

    write(store, store_path)
    print(f"{JOB} now runs at {cron}")
    return EXIT_DONE


if __name__ == "__main__":
    sys.exit(main())
