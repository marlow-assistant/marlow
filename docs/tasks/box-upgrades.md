# Box upgrades: getting new behaviour onto a box that's already seeded

**Status:** the three steps are done; the app follow-up ("Still open" 1, below) is
not built. Step 1 (the split) merged; Mike's box converted (2026-10-06):
`@import` works on the box's CLI, the `CLAUDE.md` edit is refused, `PREFERENCES.md`
edits ask the phone. Step 2 (migrations) merged; Mike's box has 0001–0003 applied
(checked 2026-10-13). Step 3 done: the Android user's box is converted (Mike,
2026-10-14), so `update.sh` keeps that identity current too.
Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

## The problem

`update.sh` updates the code and leaves the seeded working dir
(`~marlow/marlow-identity/`) alone, on purpose: Marlow and the user edit those
files. But some of what was seeded there belongs to the product, so every box
drifts from the template, and a release whose fix lives in the identity reaches
nobody who installed before it.

Found shipping to the Android user's box (seeded from an older `main`),
2026-10-05: its `CLAUDE.md` had no `[silent]` rule, so a check that found nothing
still pinged his phone; it lacked the first-run block and three smaller rules; its
`schedules.json` had no `email-flash`. The only fix was a hand diff plus pasted
blocks from `friends-alpha.md`. Every box drifts the same way, Mike's included. It
is also the second half of the root `CLAUDE.md` rule "update the live
`~/marlow-identity/CLAUDE.md` on the box too", which already cost us once when
declarative schedules shipped (2026-08-31).

## The shape: three kinds of change, three answers

### 1. Harness rules: stop copying them

`[silent]`, "always fill in Bash's description", "Not approved can mean they missed
the prompt" and "don't relay connector notices" describe how the *harness*
behaves: product code written in English. They drift because seeding copied them
into a file the user owns, like copying the schema into every row.

So split the identity. **`CLAUDE.md` belongs to the software, and `PREFERENCES.md`
belongs to the user** (Mike, 2026-10-05). The first cut had it the other way round,
a harness file imported by a user-owned `CLAUDE.md`, which left the import line in
a file the agent edits, where one careless rewrite drops every harness rule with
no error. Inverted, the software owns the file that carries the import.

- **`CLAUDE.md`**: Context, First run, Time and place, Memory, Approvals, Scheduled
  jobs, Tools, Shell commands. `update.sh` replaces it, and `FIRST-RUN.md` with it.
  The agent can't edit either (`boundaries.PRODUCT_OWNED_PATHS`, enforced in the
  hook and denied in `marlow-settings.json`); reads stay allowed, which is the
  difference from `AGENT_CONFIG_PATHS`. Its last line is `@PREFERENCES.md`.
- **`PREFERENCES.md`**: Response style plus the proactivity bullets. Seeded once,
  never replaced. Edits ask the phone, like `SOUL.md`.
- **Which file a rule goes in** (`identity/AUTHORING.md`): if it changes when the
  server changes, `CLAUDE.md`; if it changes when the user does, `PREFERENCES.md`.
- **`update.sh` only replaces `CLAUDE.md` on a box that has `PREFERENCES.md`.** A
  box without one has a `CLAUDE.md` that is partly the user's own, and gets the
  conversion below instead.

`@import` under `claude -p`: confirmed from a clean shell (Mike, 2026-10-05) for a
file next to `CLAUDE.md`. An import from outside the project may wait on an
approval a headless run can't give, which is why both files live in the working dir.

**Still open:**

1. **The app.** The avatar sheet shows `CLAUDE.md` as "Instructions", which is now
   the software's rules, and `PREFERENCES.md` isn't listed at all
   (`server/transparency.py` enumerates the files). It needs a kind, shown on both
   clients.
2. **Job prompts (`cron/*.md`): settled 2026-10-06.**
   - **A shipped job's prompt is the software's.** `update.sh` installs every job in
     its `SHIPPED_JOBS` list on every update, overwriting, and the agent can't edit
     it (`PRODUCT_OWNED_PATHS`). Not copy-if-missing: the ideas prompt was revised
     the same day (an `until` field, dates not weekdays), and a copied-once prompt
     would have frozen every box on the first version.
   - **Whether a job runs is the user's:** its `schedules.json` entry, added once by
     a migration. A prompt with no entry is inert, so overwriting never brings back
     a deleted job.
   - **A shipped prompt holds nothing personal.** It reads who is close and what is
     live from `USER.md` and memory, so learning that a sender matters means
     recording a fact, not editing the job. Named jobs run in the main
     conversation, so `PREFERENCES.md` applies to them for style.
   - **`morning.md` and `consolidate.md` are not shipped jobs** (at the time):
     seeded as starters, the user's after that. What goes in a morning brief is
     personal: Mike's `morning.md` is 120 lines to the template's 34. If a
     starter's template gets a fix every box needs, that's a migration or a note,
     not an overwrite. (`consolidate.md` later became product-owned; see
     `default-infrastructure.md`.)
   - **Mike's `email-flash.md` was personalised too** (named senders, a "Worth
     knowing" second pass). The second pass went into the shipped prompt for
     everyone; the senders needed no home of their own, since close contacts are in
     `USER.md` and memory and the shipped prompt judges "close" and "live" from
     those. Job-specific sections in `PREFERENCES.md` read oddly (Mike), so a
     sender worth flagging is recorded as a fact in memory, not a rule for one job.
   - **`ideas` shipped 2026-10-12, with one exception.** Its prompt reads "anything
     `PREFERENCES.md` names for this job" (on Mike's box, two memory/log files in
     his existing "His jobs" section): places to look, not facts about a person,
     and no memory index points at them.
3. **Mike's box: done 2026-10-06.** Its v1 `CLAUDE.md` was all personal, so the
   diff-since-seed recipe did not fit. The live file was sorted section by section:
   personal rules into `PREFERENCES.md`, software rules dropped where
   `identity/CLAUDE.md` covered them; two it covered less fully (the shell pipe
   rules, timezone vs pinned one-offs) went back into `identity/CLAUDE.md`.
   `PREFERENCES.md` was copied in before the first `update.sh`, so one run did
   deploy and conversion together.

**Converting a box:** `deploy/convert-identity.md`, written for the box owner's own
Claude to follow (sort each rule, check the first-run marker, show the owner a
`PREFERENCES.md` draft, install, check from the phone). It ships with the software,
and `update.sh`'s leave-alone message points there. It replaces the paste-blocks in
`friends-alpha.md`.

### 2. New features ship on

Mike, 2026-10-05: `email-flash` is a feature, not an idea to offer. A product would
ship it switched on because it's useful; asking "which of these 3 do you want?"
pushes the product's decisions onto the user, and most accept the default anyway.
`email-flash` is safe everywhere: a box without Gmail replies `[silent]`, which the
server drops whatever `CLAUDE.md` says. Opt-in should cover only something that
needs the user's input or consent (like connecting a new account). Nothing needs it
yet, so nothing was built; if something does, the ideas panel (`ideas-panel.md`)
has the right shape: an offer, a plan, one tap to accept.

### 3. Store migrations: numbered and applied once

Turning a feature on for an existing box means changing a store the agent owns
(`email-flash` is an entry in `cron/schedules.json`), a real data migration with one
rule a plain overwrite can't keep: **it runs once.** If a user deletes
`email-flash`, the next update must not put it back.

Built 2026-10-06: `deploy/migrations/NNNN-name.py`, run in order by `update.sh`
(`run_migrations`) as `marlow`, not root, like every other working-dir write.
Applied migrations are recorded in `/var/lib/marlow/migrations.applied`, root-owned
and outside the agent-writable tree. **Exit codes:** 0 is recorded (applied or
deliberately skipped); 75 means "not now" (the agent's store is invalid mid-edit)
and is retried next update; anything else stops the deploy before the restart.
**No "mark as applied" step on a fresh install:** every migration must leave a
freshly seeded box unchanged, and `tests/test_migrations.py` runs each against a
real seed to hold it to that; idempotent anyway (an `email-flash` entry Mike added
by hand is left alone). Store helpers are in `deploy/migrations/_store.py`, skipped
by the runner because the name isn't numbered.

- **`0001-email-flash`** anchors to the box's own `morning` entry (every three hours
  after it, nothing after 5 PM, same weekdays); a `morning` it can't read as a plain
  daily time is skipped, not guessed.
- **`0002-email-flash-evening`** adds one run three hours after the last. The 5 PM
  stop was wrong: on 2026-10-08 two time-sensitive evening alerts waited for the next
  morning's briefing. 0001 had already run on Mike's box, so it stays as it was and
  0002 extends it, the way you add a migration rather than edit an applied one. It
  only touches the shape 0001 or the seed produced; any other shape means someone
  chose those times.

Not a framework: one directory, one loop, one record file, tests. Something that
needs the user's judgement or consent can't be a migration; if one ever does, it
queues a message from Marlow instead, and that path gets built then.
