# Task — schedules that follow Mike, and changing them by asking

**Status: CLOSED 2026-08-31. Superseded 2026-08-28 by
`docs/tasks/schedules-and-reminders.md`, and both halves have now shipped** —
"change it by asking" with the store (2026-08-30), "follows Mike" with the
conversion (2026-08-31) and the drift notice that tells Marlow the store has
gone stale (same day, "The drift notice" in that doc). Nothing here is
outstanding.
The decision rule at the bottom fired: the 2026-08-25 physio cron landed at
11:57pm Halifax time, making the hand edit recurring. Read the new doc for the
design; this one is kept for the 2026-08-08 probe findings about `crontab`,
which the new design builds on rather than repeats. **Its `CRON_TZ` reasoning is
WRONG and is corrected inline below — do not build on it.**

Originally captured 2026-08-08 when the first real crontab went in. Not scoped,
deliberately not built. This records the want, what already exists underneath
it, and the rule for when it stops being speculative.

## What Mike asked for

Verbatim intent, from the session that installed the crontab:

> "while I'm on the van trip, send me things at 9:30 local time" — and then,
> getting home — "change all those back to 07:45 please"

Two separate things hide in that. The first is a schedule that tracks **where
he is**. The second is a schedule he can **change by asking**, in a sentence,
without an SSH session.

The near-term workaround, taken instead: the crontab holds server-local times
(`America/Edmonton`) chosen so they land right in Eastern, and moving them is a
hand edit. Fine for one trip. It does not survive a second one.

## What already exists

More than it looks like. **The phone already reports its timezone and the
harness already uses it:**

- `POST` from the device carries `timezone`, validated by
  `turn_context.is_valid_timezone` and stored by `pairing.set_timezone`.
- `pairing.latest_timezone()` resolves the most recently reported zone across
  paired devices.
- `server/turn_context.py` puts Mike's **local** time, zone, and full local date
  into the per-turn context block — deliberately on the prompt, not in the
  cached identity prefix.

So at turn time Marlow already knows what zone Mike is in. Nothing connects that
to *when jobs fire*. That gap is the whole task.

## Two candidate shapes

Not a decision — the two worth thinking about, with what would sink each.

1. **Marlow edits the crontab when asked.** The permission story is already
   right by construction, though not as free as first written: `crontab -l` is a
   safe read that still PROMPTS (`crontab` is not in `VETTED_COMMANDS`, corrected
   2026-08-08, so a cron turn cannot read its own schedule at all), and a crontab
   *write* also falls through to a phone prompt
   (`deploy/README.md` §6a, `docs/tasks/scheduled-work.md`). "Marlow proposes,
   Mike thumbs Allow" is exactly the human-intent call plan §4 reserves the
   ask-set for. **What needs probing first:** `crontab <file>` wants a temp file,
   and a path outside the working tree is denied by `file_policy` rather than
   prompted; `crontab -` wants a pipe, which abandons vetting and prompts (fine
   attended, auto-denied in cron). Neither is known to work today. Probe before
   designing anything.

2. **Pin the schedule to a zone and move only the zone.**
   > **WRONG, corrected 2026-08-30 — this sentence cost a shipped feature.**
   > `CRON_TZ` is a Vixie/RH cron feature. **Debian's cron, which is what the box
   > runs (`3.0pl1-184ubuntu2`), does not implement it** — not in `man 5 crontab`,
   > and the only timezone string in `/usr/sbin/cron` is `localtime`. The line is
   > accepted silently as an ordinary environment variable. Written here on
   > 2026-08-08 without checking, it was still believed on 2026-08-30 when
   > `schedules-and-reminders.md` was built on top of it, and the deployed feature's
   > timing did nothing. **The renderer must convert to system-local time itself.**
   > One `strings /usr/sbin/cron` would have caught it at any point in four months.
   Vixie cron honours
   `CRON_TZ` for the lines following it, so the crontab could express Mike's
   *intent* times once (`45 7`, not `45 6`) and a trip becomes a one-line change
   to `CRON_TZ` instead of an edit per job. Smaller diff, one place to be wrong,
   and it makes "change them all back" literally one line. Cost: the schedule
   stops tracking the server, so it needs to be right rather than merely present.

Shape 2 makes shape 1 much smaller, and is worth doing first if either happens.

**Do not build a scheduler.** `docs/tasks/scheduled-work.md` settled that, and
none of the above reopens it — a tick loop with due-time calculation and
catch-up bounds is still code replacing a default.

## Decision rule — when this stops being speculative

Build it when **either** of these is true, not before:

- Mike asks Marlow to shift the schedule in conversation and it fails (that
  failure is the probe result for shape 1, for free); **or**
- a second trip makes the hand edit recurring rather than a one-off.

Until then the hand edit is correct and this doc is the whole of the work.

## Reference

`server/turn_context.py` (why the zone rides on the prompt, not the cached
prefix), `server/pairing.py` (`set_timezone` / `latest_timezone`),
`deploy/README.md` §6a (crontab convention and the read/write permission split),
`docs/tasks/scheduled-work.md` (why real cron, and the 240s cache constraint any
new schedule has to respect).
