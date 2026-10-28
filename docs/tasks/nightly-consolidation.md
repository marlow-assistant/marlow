# Task — nightly consolidation: session rollover, memory nudge, turn retention

**Status:** **pieces 1 and 2 SHIPPED AND DEPLOYED. Piece 3 not started. Piece 4
moved to `docs/tasks/turn-retention.md`** — its resource-leak fix shipped there
(a count cap, not the privacy-motivated time window this doc originally
proposed; that framing turned out not to hold, see that doc). Design captured
2026-07-25.

Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

**2026-09-25:** the "phone session vs fresh cron session" framing below is
history. Every turn now runs in one server-owned conversation
(`one-conversation.md`); consolidation still attaches, and its retirement now
means the next turn of any kind — normally the morning brief — starts the day.

**Piece 1 (rollover) shipped 2026-08-16**, deployed the same day. A consolidate
turn that completes successfully retires the session it reviewed; the phone's
next message notices the retired hint and starts fresh. Rollover is a
*consequence of consolidation*, never an age check — an ERROR'd or skipped run
retires nothing. The same change moved retirement and the attended-session lookup
**onto disk** (`server/session_state.py`) and fixed `_gap_sentence` to key off
`resume` instead of `source == "cron"`, closing finding 1 below. Piece 2 shipped
2026-08-10.

**What forced it:** four days of real use produced **one session, 94 turns, five
days, ~4.8 MB of transcript, zero compaction** — replies taking ~40s and reading
millions of cached tokens. Nothing in the server had ever ended a session, and
the phone persists its session id *on the device*, so restarting the server
(`update.sh` included) reset nothing.

**The trap for anyone touching this again — do not reintroduce in-memory state
here.** Both halves depend on the server remembering something across a gap of
hours. Both started in-memory, on the reasoning that a missed rollover
self-corrects at the next night's run. It does not. On 2026-08-15 the log read:

    23:55:01  cron job 'consolidate' skipped: no attended (ios) session to attach to

The day's last phone turn was 05:42, the server restarted at 05:50, and by 23:55
the queue held no `ios` turn. With `Restart=always` plus evening deploys, a
restart between the day's last message and the job is **ordinary**, and both
halves fail *silently* — that log line reads like a decision the server made,
while the session it should have capped grows without limit.

**Schedule moved 23:55 → `0 1 * * *` (Mike, 2026-08-16)** so a message arriving
near the old boundary doesn't race the rollover, and so the run lands when he is
least likely to be awake in any Canadian timezone.

**Consciously declined (2026-08-16), so it doesn't get re-raised as a find:** if
Mike doesn't touch his phone for a day, the next night's consolidate re-attaches
to the already-retired session and reviews the same day again, which can write
duplicate memories. Raised in review; Mike's call was not to guard it. Revisit
only if duplicates actually show up in `memory/`.

**Sibling task:** `docs/tasks/idle-compaction.md` — the within-day half. Rollover
caps a session at a day; idle compaction keeps one *long* day cheap. Shipped and
deployed 2026-08-18.

## Where this came from

Marlow v1 ran an 11:55pm "wrap and restart" job (Mike, 2026-07-25) serving three
purposes: restart the tmux Claude session so conversations don't grow forever;
give the agent an explicit prompt to review the day and decide whether to update
memories — *"an explicit 'save memories' nudge seems helpful rather than
assuming it'll happen"*; and (maybe weekly) look for ways to be more useful. In
v2 these decompose into different mechanisms; bundling them into "restart the
process nightly" would work by accident and hide two real design decisions.

## Piece 1 — session rollover (NOT a process restart) — ✅ SHIPPED 2026-08-16

**Decision: roll the CLI session daily; do not bounce the server.** v1 needed a
restart because the session *was* a tmux process. v2 has explicit session
identity (`session_id` + `--resume`), so "start a fresh conversation" is a
routing decision. It matters more in v2 (gate1-results §8a): the injected
identity block sits at the **front of the cache prefix** with history behind it,
so a session that never ends makes every identity change re-write more and more.
A daily boundary caps that, and is the natural point for
`cache-friendly-identity-injection.md` to recompute the frozen identity.

### Corrected 2026-08-09 — the first open question was wrong

The doc asked: "a cron entry that simply stops passing the previous `session_id`,
or explicit state in the server? Prefer the former." **It never could work.**
`/internal/cron/message` enqueues with **no session hint at all**, so cron turns
were already fresh sessions every time. The only long-lived session was **the
phone's**: `ChatStore` persists `currentSessionId` and sends it as `sessionHint`
forever. Rollover is therefore not a crontab line; it is a change to who decides
a session has ended, and that should be **the server**, not the client and not
Marlow (same argument as harness-owned git commits). The client plumbing already
existed: `ChatStore` adopts a server-returned session id whenever it differs from
the one it sent (built for the dead-`--resume` retry in
`concurrent-send-session-chaining.md`).

**The expensive discovery: this blocked piece 2, not the other way round.**
Sequence: attach consolidation to the day's session *first*; rollover second.

### Design settled 2026-08-16 (Mike) — rollover is a CONSEQUENCE of consolidation

**Do not expire sessions on age. Retire a session when its consolidation turn
completes.** The consolidate job resumes the phone's latest attended session, so
the consolidate turn is by construction the last turn of that conversation. On a
successful completion the server marks the session retired; the next time the
phone sends that `sessionHint`, the server ignores it, starts fresh and returns
the new id. No client change.

Why this beats an age check:

- **It cannot roll away an unconsolidated day.** Age-based expiry and
  consolidation are independent, so a session could be retired before it was
  consolidated — destroying exactly what piece 2 exists to protect. Here the
  ordering is structural.
- **"What time?" answers itself** — after consolidate. Retirement takes effect
  on the next send, so the boundary lands in the night's actual gap. Mike
  confirmed 2026-08-16 that a message arriving after the job starts the new
  session.
- **It fails safe.** Consolidate skipped or errored means no retirement.

**The mirror-image hole, no longer load-bearing.** If consolidate skips several
nights running (phone off, job failing) nothing retires and the session grows
unbounded; that is how the 2026-08-16 audit found one at 95 turns over five
days. An age backstop would close it. **Mike's call 2026-08-16: build
`idle-compaction.md` instead** — continuous compaction bounds growth independent
of consolidation, making a skipped night non-critical. The backstop remains a
cheap belt-and-braces option.

**Why 01:00 box-local (Mountain).** Not politeness — 23:55 is already inside
Mike's quiet window. The job now **rolls the session over**, so firing it
mid-conversation resets the thread under him. The metric is margin to his nearest
observed activity: from 117 attended turns, zero between 22:00 and 03:00,
earliest ever 04:36, latest ever 21:32. He is an early riser, so **travelling
east is the binding case**: at 02:00 the Toronto/Ottawa margin is 36 minutes and
Halifax is already awake; at 01:00 it is 1h36m and 36 minutes. A hand-picked
compromise hour, correct **until he is east often enough that it trips**, at
which point it is `schedule-timezone-following.md`. Crossing midnight means
**`cron/consolidate.md` must stop saying "today"** (the day to review ended an
hour ago; that prompt is personal content on the box, Mike's edit) and sharpened
finding 1 below.

Remaining open question: does anything need to carry across the boundary beyond
what `memory/` holds? If routinely yes, memory isn't capturing enough — worth
noticing rather than papering over with a longer session.

## Piece 2 — the consolidation turn — ✅ SHIPPED 2026-08-10

An explicit nightly prompt asking Marlow to review the day and update `memory/`.
A discretionary "remember things when relevant" loses to answer-momentum (the
failure mode that motivated injecting identity at all,
`loader-identity-injection.md`), and memory writes are *the* cache-invalidation
event for the injected block, so batching them at the session boundary costs
nothing extra.

The design problem: cron enqueues with no session hint, so a naive nightly turn
would run in a fresh session with no visibility into the day it reviews — worse
than a no-op, since `WRITABLE_SUBTREE=memory` lets it write plausible invented
memories **unattended, silently, with no phone prompt**. The fix:
`/internal/cron/message` attaches this job (and only this job — a server-side
set, not a request flag, so a hand-edited crontab typo can't revert it to blind)
to the `session_id` of the phone's most recent completed `ios` turn, and **skips
the job entirely, visibly logged**, when no attended session exists. That skip is
the load-bearing decision that makes the job safe to run unattended. A review
finding (`require_session`) closed a gap where the dead-session self-heal in
`invoke.py` could silently undo the attach.

**Constraints for anyone touching this job again:**

- Scoped to `memory/` only: `file_policy.WRITABLE_SUBTREE` is `"memory"`, so a
  write anywhere else auto-denies at midnight every night. Don't widen the
  prompt to "project docs" without a separate `WRITABLE_SUBTREE` decision.
- It writes files and must not commit or push; the harness auto-commits
  separately (`marlow-working-dir-git-policy.md`), and `git commit` would
  auto-deny nightly.
- It is the most expensive turn of the day by design — a cold read of the full
  day's history hours past any cache window. Expected, not a regression.
- `cron/consolidate.md` is personal content authored on the box, never in this repo.

**Findings from the `/code-review medium` on that change:**

1. **The elapsed-time turn-context sentence never reached this job** —
   `_gap_sentence` suppressed "N hours have passed" for `source == "cron"`, false
   for `consolidate`, which resumes a real session. **Fixed 2026-08-16**, keyed
   off `resume`.
2. **`require_session=resume` in `cron_message` is an implicit coupling.**
   Correct only because the attach block is the sole `resume=True` caller.
   Revisit once a second exists. Still open.
3. **The dead-session fake-`claude` test fixture is duplicated 3×**
   (`tests/test_invoke.py` ×2, `tests/test_queue.py` ×1). Cosmetic; fold in when
   next touched. Still open.

## Piece 3 — the proactive review: NARROW IT BEFORE BUILDING

Deliberately deferred, and reshaped. The "agents write their own skills" framing
is speculative; asking a model to introspect about its usefulness from a cold
start tends to produce plausible-sounding filler.

**The tractable version uses data the system already has:** permission denials
(`permission_denials`, already recorded) and usage telemetry
(`docs/tasks/usage-telemetry.md`). *"What did I try to do that I wasn't allowed
to do, and what did Mike ask me to repeat?"* is a grounded question with real
inputs and yields concrete suggestions — boundary adjustments, a cron that should
exist, a recurring task worth automating. **Hold this until piece 2 has run for
several weeks** and there is a corpus.

## Piece 4 — turn retention

**DONE — moved to and shipped in `docs/tasks/turn-retention.md`.** The actual bug
(`WorkQueue._turns` never evicted, unbounded on a `Restart=always` service) is
fixed there with a plain count cap; this doc's privacy/time-window framing didn't
hold, since the CLI's own session store already persists conversation content
independent of `_turns`. That doc also carries the still-open orphaned-held-turn
gap and Mike's multi-device-history idea.

## Reference

`docs/marlow-v2-gate1-results.md` §8a (why the session boundary matters);
`docs/tasks/cache-friendly-identity-injection.md`; `docs/tasks/usage-telemetry.md`
(feeds piece 3); plan §7 gate 5 (cron cutover) and §4 (cron never-prompt policy).
