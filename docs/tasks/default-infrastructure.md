# Task — default infrastructure: what the harness runs, what the user owns

**Status (2026-10-23): steps 1–3 shipped on both clients, merged after the
morning-after checks all passed (Mike, 2026-10-23), on Mike's and Taylor's
instances. Step 4 (the seeded `morning.md`) is not started.** Every product
question was decided by Mike on 2026-10-21 ("Decided", at the bottom).
Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

## As built: where it differs from the design below

Where the design sections disagree with this list, this list is what the code does.

- **Steps 2 and 3 share one migration, 0007,** which hands over all three harness
  jobs (`ideas`, `consolidate`, `email-flash`). One per step would deadlock: the
  validator refuses all three, so a migration removing only some would find the
  store invalid and defer forever. It can't use `_store.load` for the same reason.
  It seeds before it can defer, because the validator's message tells Marlow to
  delete these very entries, and a day seeded only on success would go with them.
  It logs each removed entry's old cron, in case someone moved it on purpose.
  The preferences file is in `data_dir`, hidden from the agent by bwrap, not by
  Unix permissions (the server and the migrations both run as `marlow`, so one
  script does both halves); `update.sh` runs migrations with the server stopped,
  or `PreferencesStore` re-reads the file, so a cached copy can't overwrite the seed.
- **Seeding.** `day_end` = the `consolidate` entry's time − 2h, `day_start` = the
  `morning` entry's time, when plain daily times, else the defaults; if the seeded
  day fails validation, log it and leave preferences null. Nothing moves on
  upgrade (`tests/test_migrations.py::test_mikes_box_moves_nothing`). The check-in
  interval is the step between the `email-flash` hours when evenly spaced every
  day, else 3. With no entry it is 0 (off) only on a box that has been paired
  (`device_tokens.json` exists): the installer runs every migration before anyone
  pairs, and the fresh seed has no entry either.
- **The check-in rule:** start + N, start + 2N, ... while at least 2h of the day
  is left (`schedules.CHECKIN_MIN_BEFORE_END_MINUTES`), measured from the start so
  a day ending after midnight works the same way.
- **A rejected store keeps the last good crontab,** harness lines included, rather
  than rendering harness-only (which would drop every reminder over one bad edit).
  The cost: a Settings change made while the store is broken waits for the fix.
  Marlow is told about the broken store every turn. There is no "last good
  timezone" file: with no store at all, harness lines render in the server's local
  time. **Open:** a *deleted* store now installs harness-only, so the user's own
  entries stop firing (before, a missing store left the crontab alone). Whether
  deleting should keep them, as a rejected store does, is undecided.
- **`0001`, `0002` and `0003`'s behaviour tests are retired**: each only ever added
  or retimed an entry that is now invalid. The fresh-seed check still covers all three.
- **Names in code.** `schedules.PLUMBING_JOBS` (`ideas`, `consolidate`) is tier 1:
  tagged `tier: "plumbing"`, never delivered. `schedules.HARNESS_JOBS` adds
  `email-flash`: every job the renderer adds itself and no entry may name. The
  check-in is tier 2: harness-timed, delivered as before, `[silent]` as before.
- **Settings shows "Your day" once the box has answered**, on both clients. A
  failed load leaves it out until Settings is next opened; a box too old to send
  `checkin_every_hours` hides only that row.
- **The aging step ships to everyone** (Mike, 2026-10-21). Mike's live
  `cron/consolidate.md` had grown a "1. Age the dated plans" section after the
  2026-09-08 nudge. It's general: any forward-dated plan goes stale on a quiet day
  and nothing else corrects it then. The shipped prompt carries a name-free
  version that `update.sh` installs. So far it has only run on Mike's box: read its
  Activity line on other boxes' first nights.
- **Activity shows background jobs by default** (Mike, revising decision 1 the
  same day: "Activity should default to transparency").
- **Parked:** the Schedule panels no longer list the three harness jobs; on
  desktop web, typing a time can save the in-between values; the deleted-store question above.

## Where this came from

A second user on Mike's box didn't want the 01:00 consolidation message on their
phone; Mike does. The stopgap (their `cron/consolidate.md` replying `[silent]`)
worked, but pulling on it showed a bigger problem:

- `consolidate` is **infrastructure** stored as a **user preference**. Session
  rollover (`RETIRING_JOBS`), the memory pass and the timing of `ideas` all depend
  on it, yet it's an entry in `cron/schedules.json`, which Marlow owns and edits,
  next to "bins out Monday". If a user says "stop sending me that 1am message",
  Marlow could just as easily delete the entry. Every check would pass, and the
  conversation would stop rolling over and being consolidated.
- Fixing it with an identity line ("never remove consolidate") was rejected
  (Mike): it leaves the system depending on the model obeying, the fragility being
  removed. `ideas` and the check-in have the same shape, as do the identity's
  "move X with Y in the same edit" rules: harness couplings written as
  instructions to the model.

The aim: users change **what** Marlow does for them and **when**, never **how it
keeps itself running**.

## The three tiers

| Tier | Jobs | Who schedules | Prompt owned by | Reply goes to | User can |
|---|---|---|---|---|---|
| 1. Plumbing | `consolidate`, `ideas` | harness, from "Your day" | product | activity screen only | see output, if they choose to |
| 2. Built-in feature | check-in (`email-flash`) | harness, from "Your day" | product | phone, as today | turn off, change frequency |
| 3. The user's own | `morning`, `week-ahead`, reminders, custom jobs | `schedules.json` | user (seeded starters) | phone, as today | everything |

**A prompt that's mostly taste belongs to the user; a prompt that's mostly
mechanism belongs to the product.** The morning brief is all taste: the user's
file, seeded with a good default, with no `PREFERENCES.md` overrides layered on
top. Mike found that pattern clunky in practice: one thing split across two files
that disagree about which wins, and the edit always goes to the wrong one. The
check-in prompt is mostly mechanism (two passes, `[silent]` rules, Gmail-missing
handling), so the user gets knobs, not the prompt. The accepted cost of tier 3: a
better seeded `morning.md` reaches only new boxes. If that ever matters, a run-once
migration can *offer* the new version through Marlow; it never overwrites.

## "Your day": the one setting that times the harness

Nothing told the harness when the user's day is; it guessed (tidy-up at a fixed
01:00, check-ins counted from whenever `morning` happens to be). Mike's two
questions, "when should Marlow tidy up at the end of the day?" and "when should
Marlow be working on your behalf?", both reduce to **when does your day start and
end**, so it is one setting plus **Check-ins: every N hours (or off)**. Two rows in
Settings on both clients. Neither is a mechanism: anyone can answer without
knowing how Marlow works. That is the test for what earns a place on the Settings
screen; cron times and job names don't pass it.

- **Tidy-up runs after the day ends:** `ideas` at end + 1h30, `consolidate` at
  end + 2h (the `ideas`-before-`consolidate` ordering becomes two offsets in the
  harness, not a sentence in the identity). Defaults (end 23:00) give today's
  00:30/01:00, so nothing moves on Mike's box.
- **Check-ins run inside the day:** at start + N, start + 2N, ... while at least
  2h before the day ends. 08:00–23:00 with N = 3 gives 11, 14, 17, 20, Mike's live
  schedule.
- **The morning brief is NOT derived.** It's tier 3. Onboarding seeds it at the
  day's start, but changing the day later doesn't move it; the server doesn't move
  a file the user owns behind their back.

**The day may end after midnight** (Mike, 2026-10-21: a night-owl user's "my day
goes until 2am" must work). Validation: start 04:00–12:00; end 20:00–03:00 (next
morning); the night, end → next start, at least 4h (room for both tidy-up turns
plus margin; a 02:00 end with 08:00 start passes, 03:00 with 06:00 doesn't). Times
are local to the store's `timezone`, so the wall-clock day travels with the user.

**Where it lives:** `server/preferences.py`, beside face and effort, via
`/v1/preferences` (`day_start`, `day_end`, `checkin_every_hours` 1–6 or 0 = off;
null means default 08:00/23:00/3, so a box that never opens Settings behaves like
today). A successful POST re-renders the crontab; the validator rejects the whole
POST on a bad value. **Not in `schedules.json`, and not writable by Marlow:**

- **One writer per value.** If the app and the agent can both write it, they
  disagree and the user sees a setting that changed by itself.
- **It times plumbing.** The end of day drives session rollover, which is why
  consolidate left `schedules.json`; moving its time into another agent-writable
  file would undo the point.
- **Marlow still knows it:** `turn_context` carries the day and frequency, so
  "check in less often" gets pointed at the setting. The cost is that "just tell
  Marlow" doesn't change it directly: right for a value that runs the overnight jobs.

## Tier 1: plumbing

**Scheduling.** The renderer (`schedules.render`) adds the plumbing lines itself,
reusing the DST-safe conversion in `_recurring_lines`, the entrypoint and
`/internal/cron/message` with `job=`, so `ATTACHED_SESSION_JOBS`, `RETIRING_JOBS`,
the effort locks and the queue don't change. An in-process poll loop like
`idle_compaction` was the alternative; it would duplicate the timezone handling.
Plumbing must not depend on the store being valid (one bad entry rejects the file,
fine for reminders, not for the job that rolls the session), the lines are not
counted against `MAX_DAILY_FIRINGS`/`MAX_ENTRIES` (those bound what the model
schedules), and they render from defaults even if the preferences file is
unreadable. A user job named `consolidate`/`ideas` in `schedules.json` is a
validation error, or the harness and the store could both fire it.

**Prompts become product-owned.** `consolidate.md` joined `SHIPPED_JOBS` and
`boundaries.PRODUCT_OWNED_PATHS` like `ideas.md`; otherwise Marlow could still
empty the prompt though it can't delete the schedule. Mike's live copy was diffed
2026-10-21; the only material difference was the aging step, now in the template.

### Replies are never pushed

The tier decides delivery, not the reply text. A plumbing turn that finishes DONE
gets no transcript row and no push, whatever it says (the `silent` check in
`queue.py` gains "or the turn's job is plumbing"). An ERROR turn still pushes: a
failed consolidation is the one thing the user must hear, because yesterday's
conversation carries into today. The prompts now write for the activity screen
(`consolidate.md`: a short factual line on what changed, or "nothing worth keeping
today"; `ideas.md`: one line on what was added or retired, not `[silent]` nightly).
This also settles the second user's request for everyone: **housekeeping shouldn't
announce itself at 1am.** The 01:00 message was always a diagnostic; Mike, the
operator who wants it, keeps it on the activity screen.

## Seeing plumbing output (both clients)

Mike's requirement: power users can see what the silent jobs output, **if they
want to**, on the activity screen.

### Where the text lives

The activity log can't hold it: "no prompt, no reply text" is part of its design
(`activity_log.py`, `ux-lane.md` §2a). The chat holds reply text linked by
`turn_id`, but a plumbing reply has no chat row. So a third home:
`data_dir/background/YYYY-MM.jsonl` (`{turn_id, label, finished_at, text}`), written
in `_drain` beside the activity record with the same `O_APPEND`, never-raises shape
and 12-month retention. `data_dir` is in `SECRET_PATHS`, so Marlow can't rewrite
what it reported. `GET /v1/activity/{turn_id}/output` is device-token gated, 404
when none, fetched on expand so `/v1/activity` keeps its size and shape and
reply text stays out of a response that never had it. **Rejected: a hidden
transcript row** (`visibility: background`): the transcript feeds chat sync, and an
older client that doesn't know the flag would show it as a message at 01:00, the
thing the second user asked not to see. New reply text over the API is a new
endpoint, so `/security-review` runs as well as `/code-review high`, though it is
the same class of data the transcript already serves to the same token.

### The activity record

The record gains `"tier": "plumbing"` (additive; `RECORD_VERSION` stays 1; absent
means not plumbing) and `"has_output": true`. Facts the harness knows, not model claims.

### The screen

- **A "Show background jobs" toggle**, **on by default** (Mike, 2026-10-21:
  Activity defaults to transparency): a per-device client preference (iOS
  `@AppStorage`, web IndexedDB kv `KV_SHOW_BACKGROUND`), not server state, since
  "if they want to" is a viewing choice and two devices may differ.
- Plumbing rows get a quiet "background" treatment; expanding shows the tool and
  file lines plus the fetched output.
- **Failures show regardless of the toggle.** A plumbing row whose status isn't
  DONE always appears. A hidden failure is the one thing the toggle must never cause.
- Titles "Nightly tidy" and "Overnight ideas", never job ids (software names don't
  reach the user).
- **Collapse repeated detail lines** (Mike, 2026-10-21): "Read memory/travel.md ×3",
  "Command ×4", merged by label and target in first-seen order. **Fixed in the
  clients, not the server:** the record is a true log of each call with its own
  `at`, merging would lose the count and timing to fix a display issue, and 12
  months of stored records already have the repeats. Pure function with tests on
  both sides (`activityFormat.ts`, `ActivityFormat.swift`).

## Tier 2: the check-in

It was an `email-flash` entry whose `cron` Marlow had to keep in step with
`morning`. Now it **leaves `schedules.json`**; the harness renders it from "Your
day" and `checkin_every_hours` (0 renders nothing; turning it off is a real user
choice here, unlike tier 1), with **no coupling to `morning`** (the identity rule
goes away rather than moving into code). Same render properties as tier 1: outside
the firing budgets, rendered even when the store is rejected. The job id stays
`email-flash` in code; the user-facing name is "check-in". `[silent]` keeps working.

## Tier 3: the user's own

No mechanism change; `morning.md` and `week-ahead.md` stay seeded starters.
**Open (step 4): tighten the seeded `morning.md`** to a short, scannable shape with
a rough length budget: every new box starts from it, and Mike's own brief is four
times the template (his is fixed by asking Marlow to cut it). The identity stops
suggesting `PREFERENCES.md` as the place to change a user-owned job; it's changed
by editing that job.

## Identity changes (same PR as the harness change)

Per CLAUDE.md: the "move `ideas` with `consolidate`" and "move `email-flash` with
`morning`" rules were removed; one paragraph says overnight upkeep and the
check-in are run by the software, timed from "Your day" in Settings (which Marlow
can't change, so it points users there), and shown on Activity under background
jobs. That paragraph has to exist, because a Marlow told nothing will go looking or
add a `schedules.json` entry that double-fires a check-in.

## Order of work

1. **Visibility first:** the background store, the endpoint, the `tier` field, the
   toggle and collapsed detail lines, on both clients. Harmless alone, and it must
   land before step 2 or Mike loses his 01:00 signal before he can see it anywhere
   else.
2. **"Your day" + tier 1:** preferences fields and Settings rows on both clients,
   render path, delivery rule, product-owned `consolidate.md`, prompt wording,
   migration, identity. One PR, one release; half of it double-fires or drops jobs.
3. **Tier 2:** the check-in from "Your day".
4. **The seeded `morning.md`**, whenever.

Rollover still happens when the reply is `[silent]`: `queue.py` retires on DONE,
independent of delivery, and errors still push.

## Decided (Mike, 2026-10-21)

1. **Background jobs are shown by default for everyone** (revised same day: first
   decided hidden, then "Activity should default to transparency"). Hiding them is
   the per-device opt-out.
2. **`ideas` can't be turned off.** A core Marlow feature, tier 1 with no knob; a
   user who ignores the Ideas panel costs one quiet turn a night.
3. **`week-ahead` is the user's (tier 3)**, a seeded starter like `morning.md`.
4. **Overnight timing is a user preference, not a fixed time:** one "Your day"
   setting times both tidy-up and check-ins, in the server-private preferences
   store, shown in Settings on both clients.
5. **The day may end after midnight.**
