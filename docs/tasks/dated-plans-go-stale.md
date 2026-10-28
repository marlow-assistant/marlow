# Task — a dated plan block outlives its days, and every reader trips on it

**Status:** **both halves SHIPPED and OBSERVED WORKING on the box (checked
2026-09-11, ~48h in).** Gates 1, 2 and 4 passed on their first real outing; gate
3 had not had a chance to fire yet and is the only thing still open. The
reasoning below is the record of why, not an open question. Place and trip
details are described by shape, since they came from Mike's real memory files.

Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

## What the box showed (2026-09-11, 48h in)

**Gate 1 — consolidate opens the file. PASSED.** No `num_turns: 1` run since the
fix; the cron entries in `usage.jsonl` are 8, 9 and 12 turns. The failure mode was
an early exit at zero tool calls, and it is gone.

**Gate 2 — the plan block ages. PASSED,** on the live test this doc set up. A cron
commit to the identity repo took the stale "Tonight" bullet out of the plan block
and rewrote it into the trip log as an unconfirmed day, ending with "No
confirmation on file that [the two days] went as planned — quiet day, no
conversation to check against." The part worth noticing: it aged a day it had *no
conversation about*, and said so rather than inventing the day or leaving the
bullet where it was. That is the case the old prompt got wrong and Marlow's own
written diagnosis defended as correct. The plan block left behind carries no
dates at all now.

**Gate 3 — the nudge stops needing arithmetic. NOT YET TESTED.** The next
Sep 10 evening nudge did trip again on the same stale bullet, but it fired at
20:27 and the fixed consolidate did not run until 21:30 (the cron commit that
aged the plan block, gate 2), so it read a pre-fix file. Not a counter-example. The real test needs a plan block holding both a past
and a present day at nudge time. Standing ordering: consolidate runs 01:00
Mike-local and the nudge 20:57 Mike-local, so the nudge always reads a block aged
~20h earlier that same day, the correct side of the fence.

**Gate 4 — Marlow offers to retime the store. PASSED, first attended turn.** The
schedule store's `timezone` was changed in an identity-repo commit made by an
**ios** turn. Unprompted: Mike's message was only about the day's campground
plan; Marlow answered that and then added, on its own, that the cron store was
still set to the old zone while he was on the new one, so every scheduled job was
firing 30 minutes early and would not self-correct, and asked whether to retime
it. It asked first and edited only the `timezone` field.

**So the escalation flag is NOT earned, and should not be built.** This doc had
argued it was "close to earned already" on two days of silence and said to give
the wording until ~Sep 17 before building state. The wording was sufficient. What
changed was not stronger adjectives: it was going from a bare fact to a sentence
carrying the cost, the named one-field repair, and the trap to avoid — the same
four parts as `_rejection_sentence`, the second time that shape has worked
unaided. **Prefer that shape over new state next time.**

Caveat: this passed on the first attended turn after a zone change, the easiest
possible case. It does not show the notice still lands on day five of being
ignored. That scenario, the one the escalation flag was for, is unmeasured rather
than disproven.

## The incident

2026-09-08, 20:27 local. The physio nudge told Mike his plan for a night three
days earlier had him overnighting at a place he had left long before; he was
three days and a ferry crossing past it. He asked Marlow what happened the next
day and got two explanations in a row, both invented:

1. "A stale memory bug — `consolidate` silently failed to fire Sep 7/8." Wrong.
   `marlow-server.log` has `cron job 'consolidate' enqueuing a turn` at 21:30 on
   every night in the month.
2. After Mike pushed back: "consolidate only has that day's conversation to draw
   from, and on days with no chat there is genuinely nothing for it to learn."
   Also wrong — Sep 8 had a full day of conversation, 05:24 to 14:35, and it
   *included Mike arriving at the next stage of the trip*.

The second explanation was written into `memory/future-marlow-improvements.md` on
the box. **It is wrong and should be corrected there**, or the next session will
build the wrong fix. That entry is why this doc leads with forensics.

## What actually happened

The nudge transcript settles it. It had the correct date, read the correct file
(`health.md`, `travel.md`) and picked the wrong bullet. `travel.md` (written by
consolidate two nights earlier) held a "Current plan" block of day-by-day bullets,
the first dated three days past, formatted identically to the live day, with the
most narratively salient place name in the first bullet. **The file was correct;
the right answer was four lines below the one it used.** `thinking_tokens: 15` on
that turn — it didn't do the date arithmetic, it pattern-matched the block's
opening.

So the failure is not stale memory, a missing tool or a broken job. It is that
**a forward-dated plan block does not distinguish itself from a past-dated one**,
and every consumer must filter it by date, correctly, every time. Three consumers
do this (the nudge, the morning brief, Marlow in conversation) and one will always
get it wrong.

### Ruled out, with the evidence

- **Cron/session continuity.** True that a nudge has none —
  `ATTACHED_SESSION_JOBS = frozenset({"consolidate"})`, so every other job runs
  `resume=False`. Deliberate (`morning` inheriting last night's conversation is
  the failure the opt-in set prevents). Not what happened: memory was correct and
  the nudge read it.
- **Model/effort.** Sonnet at medium, but with **15 thinking tokens**. The next
  night's consolidate, same model and effort, spent 1408 across 9 turns and fixed
  the file properly. Not a capability ceiling.
- **Idle compaction.** The Sep 8 attended session was compacted twice before
  consolidate resumed it (281,313 → 60,446 tokens, then 211,350 → unmeasured).
  **Mike's call, 2026-09-10: not pursued.** Compaction trims well, ~38K is still
  a lot, and a 287K prefix is more likely web pages nobody expunged than
  conversation worth keeping. Fix the job instructions first and see whether
  compaction ever shows up as a symptom on its own.

## The real defect: consolidate exits before it opens a file

Sep 7 and Sep 8 consolidate runs both had `num_turns: 1` — **zero tool calls.
Neither opened `travel.md`.** Sep 8's entire reply called the day's arrival at the
next stage "transient, not durable" and recorded nothing. An arrival is a
trip-log event; the day the trip moved, consolidate classified the move as
transient.

This was the prompt working as written, not the model going off-piste: the whole
frame was *"review today's conversations and decide what's worth remembering"*;
the last line was an explicit early exit (*"If nothing today is worth keeping, say
so in one line and stop"*) reached before any file was opened; and the
dated-file paragraph was doubly conditional (*"**before appending**, reread the
file"*, so never if not appending; *"**if today moved the plan**"*, so the
conversation had to describe the move). None of it was about **elapsed time**. The
plan didn't need to *move*; it needed to *age*, and nothing asked anyone to check
the calendar against the file.

## The fix

Give consolidate one unconditional, mechanical step that runs **before** the
"worth remembering" judgment and can't be short-circuited by it:

> Open every memory file holding a forward-dated day-by-day plan. For each
> entry whose day is now in the past, move it into the file's log section
> rewritten as what happened, and delete it from the plan block. When you are
> done, the plan block contains today and later and nothing else.

That makes the invariant structural rather than interpretive: **the first bullet
of a plan block is always current**, so no reader filters by date and the nudge's
failure mode stops existing rather than becoming less likely. Two properties kept
deliberately: **it runs on days with no conversation** (the calendar moves whether
or not Mike narrated it), and **the early exit moves below it**, so a consolidate
reply reporting no changes and showing no file reads is now a visible failure
rather than a plausible one.

**Where it lives:** `cron/consolidate.md` on the box under `~/marlow-identity/cron/`,
**not in this repo** (job prompts are personal content; `deploy/README.md` §6a,
`docs/marlow-v2-identity-architecture.md`). A session reading only the repo cannot
see the prompt that shipped; the box is the source of truth.

## The other half: Marlow never offers to retime the schedule store

Raised by Mike in the same conversation. He had been in a new timezone since
Sep 8; `cron/schedules.json` still held the old zone, so every recurring job fired
30 minutes early. The harness had *said* so on every turn since 2026-08-31 and
Marlow never once offered to fix it.

The obvious read was that the sentence was too weak: a bare statement of fact, one
clause among several on turns about something else — no cost, no remedy.
`_rejection_sentence` in the same module says what is broken, **what it costs,
what a repair looks like, and which trap to avoid**, and demonstrably works
(Marlow diagnosed and repaired a broken store unaided from that sentence alone).
So the drift sentence now has the same four parts, and a test pins each one.

**But the other channel was checked first.** `identity/CLAUDE.md` and the live
copy already said *"When the per-turn context block says the store's zone
disagrees with Mike's, OFFER to update it; never change `timezone` silently."*,
read every turn. For two days Marlow held both the fact and the instruction and
did nothing: **the information was never missing**, so the identity needed no
change (a third copy of an instruction two copies failed to trigger is the move to
avoid). What was left was attention: an ambient notice about a 30-minute drift
loses to whatever Mike asked about, every turn, and nothing makes it more urgent
than the turn before. The wording shipped anyway, but the expectation was that it
wouldn't suffice. Gate 4 above shows it did.

**The ask is gated on attended turns** (`active_turn.ATTENDED_SOURCES`, the same
gate `invoke.py` uses for streaming). Found by `/code-review medium`: the block
goes on *every* invocation, so an ungated imperative would put "offer to retime
the store, ask first" inside a bedtime nudge whose reply goes to the lock screen,
in a job that tells itself nobody is awake to approve anything. The bare fact was
inert enough to ride along; an instruction is not. Cron keeps the fact and the
cost (consolidate can act on it by writing memory) and loses only the half that
needs a person in front of it.

**The escalation that was not built:** a "has this been raised yet" flag — state
the notice once, loudly, on the first attended turn after a zone change, then fall
back to the quiet form. It is state, against this repo's bias for machinery on an
unmeasured edge case; the wording was tried first, and was enough (gate 4 passed
on the first attended turn, about 2 days in, before the ~Sep 17 deadline). If the
notice is ever observed ignored on day five, that is the signal to build it.

## How we knew

Both halves were prompt wording with no test able to prove them, and this repo
has a recurring false-pass problem, so the gate was the box: (1) consolidate's
`num_turns` in `usage.jsonl` is no longer 1, including on a no-conversation night;
(2) `git log -p -- memory/travel.md` shows elapsed days moving into the log
without Mike narrating the move; (3) the nudge references the present plan day
when a block holds both past and present (still untested); (4) Marlow offers to
retime the store on an attended turn. A live test for (2) was left deliberately
un-hand-fixed so the next run had something past-dated to age.

## Related

`docs/tasks/nightly-consolidation.md` (rollover and attach; this doc is about what
the job *does* once attached); `docs/tasks/memory-file-shape.md` (argues the
structural-and-cheap pass over memory, which step 1 here applies to one file
shape); `docs/tasks/schedules-and-reminders.md` (where the drift notice came
from; read before touching cron).
