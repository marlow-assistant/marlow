# Task — memory file shape: rewrite, don't stratify

**Status: rules SHIPPED 2026-09-04** to `identity/CLAUDE.md` (template) and the
live `~/marlow-identity/CLAUDE.md` on the box, same change. **One item parked
with a decision rule** ("Active documents in `memory/`", below). A lint is
sketched and deliberately not built.

Genesis: Mike noticed Marlow giving him a plan that disagreed with itself, asked
about it, and Marlow re-read the file and reconciled — correctly, and only when
asked.

## What the git history actually showed

Worth reading before changing any of this, because the obvious diagnosis is
wrong and the wrong one leads somewhere useless.

`memory/travel.md`, four commits in three days, **every one a targeted replace**:

| edit | source | what |
|---|---|---|
| 1 | cron | deleted the stale Sep 4 / Sep 5 lines |
| 2 | ios | deleted the stale Sep 2 / Sep 3 lines |
| 3 | cron | extended one line, added the next day |
| 4 | ios | deleted the stale Sep 6 / 7 / 8 lines |

So **it is not an append-blindly bug, and not an `index_only` visibility bug.**
The first theory here was that the frontmatter `description:` is read from a
bounded prefix (`server/identity.py:107`) while appends land at the bottom, so
the index could never see them. True of the mechanism, and not what happened —
the file was being read and reconciled every time.

**The real defect is emergent across the edits, not present in any one of them.**
Nine sibling bullets had accumulated under a single heading:

    **East coast + return leg sketch (drafted 2026-08-29, from Gander):**

...followed by `Revised on the day (Aug 31, weather-driven)`, `Locked plan,
confirmed by Mike (Sept 1)`, `Superseded plan (kept for reference, not
followed)`, `Revised (discussed Sept 2)`, `Sep 3 (today, actual)`, `Sep 4
(planned)`, `Revised (Sep 4 evening)`, two never-cleaned past days, and one
bullet whose *entire content* was a note explaining which other two bullets had
been deleted — a hand-written diff, in a file the harness commits every turn.

Answering "what is the plan for tomorrow" had come to mean reading all nine and
applying supersession rules stated in prose, inconsistently (one bullet
explicitly kept-for-reference, two stale and unmarked, later-wins everywhere
else).

**The heading is what caused it.** A section named for the moment it was drafted
is an artifact, and you annotate an artifact rather than rewriting it. Every
correct local reconciliation therefore landed as a new stratum. The same content
under a heading that names the *thing* rather than the *moment* reads as state,
and state gets overwritten.

**No model would have caught this at edit time,** which is the point worth
carrying: each edit was locally right. This is not an argument for a more
capable model on the write path.

## The rules that shipped

In the identity's `## Memory` section, four lines. They are cheap only because
the harness auto-commits `MARLOW_WORKING_DIR` every turn and Marlow keeps read
git access (`marlow-working-dir-git-policy.md`) — the history is already stored,
attributed and timestamped, so keeping it inline is redundant.

1. **Rewrite, do not stratify** — edit the lines a change makes wrong; no dated
   "Revised" bullet beside the stale one; never a note saying what was removed.
2. **Current state first, history below**, under a heading that says it is not
   current.
3. **Do not date a section heading.** This is the causal one; the other three
   are hygiene.
4. **Move the substance, move the `description:`.** Under `index_only` that line
   is the entire per-turn view of the file.

## The consolidate prompt was part of the cause

`cron/consolidate.md` on the box (personal content, never in this repo) already
carried a paragraph for exactly this failure, added after an earlier incident. It
told Marlow to reread the whole file before appending and to reconcile disagreeing
dated entries — **which it did, every night, correctly.** The wording that failed
was the remedy it prescribed: *"strike or fold it in as superseded."* Folding in as
superseded is what produces a `Superseded plan (kept for reference, not followed)`
bullet. The instruction generated the strata it was written to prevent.

Rewritten 2026-09-04, in two parts:

- **Delete, don't mark.** The harness commits after every turn, so git holds each
  earlier version; a file carrying its own edit history is strictly worse than one
  that says what is true. (Deliberately does not tell it to run `git log` — cron
  denies most shell, and this job is told so in its own opening paragraph.)
- **A shape check, scoped to the files today touched.** Named signs — a stack of
  `Revised (date)` / `Locked plan` / `Superseded` siblings, a bullet describing what
  used to be there, a heading naming the day it was drafted — and the rewrite to
  perform. Not a nightly audit of the whole directory.

Guarded, because this job runs unattended at 01:00 with `WRITABLE_SUBTREE=memory`
and no phone to approve anything: *move facts, don't drop them; if unsure whether
something is live, keep it and say so in the reply.* Git is the real backstop —
every prune is recoverable and attributable.

**Why the job and not just the identity.** The identity rules are turn-scoped:
they stop the *next* edit from stratifying. They cannot catch a defect that only
exists across nine edits, because no single turn sees it. Pruning needs a
scheduled moment, and consolidate already is one — nightly, resumed onto the day's
session, already scoped to `memory/`. This is the same asymmetry argument as
below, applied: the disposition can't be relied on, so it becomes a job.

## Parked — active documents in `memory/`

An itinerary edited daily from a phone, mid-trip, is not a durable fact; it is a
working document living in a store built for things with a long half-life.
`travel-docs/` already exists beside it.

**Decision rule: revisit only if the rules above fail to hold the shape.** Check
`travel.md` after the current trip ends — if it has re-stratified despite rule 3,
the frame was not the cause and the file needs a different home. If it reads
clean, this stays parked permanently. Do not build a second store on the strength
of one file.

## The lint, sketched and not built

The failure has a mechanical signature: bullets matching
`Revised|Superseded|kept for reference|used to sit here|\(was ", and dated lines
whose date is in the past. A grep over `memory/` would have flagged `travel.md`
days before Mike did.

**Not built, deliberately** — one instance is not a pattern, and the rules above
are the first thing to try. If the shape degrades again after a month, this is
the cheap detector, and it belongs wherever the periodic pass ends up rather
than as its own machinery.

## What this settles about weekly reflection

`nightly-consolidation.md` piece 3 and the "weekly Opus dreaming" idea
(2026-09-04) both wanted deep synthesis across the week. This failure is
evidence for a *periodic whole-file pass* and evidence **against** depth being
the lever: the per-turn behaviour was correct, and what was missing was anyone
looking at the whole block at once.

The generalisation, which applies to this repo's own docs as much as to
`memory/`: **adding is always locally correct; pruning is only ever globally
correct.** A turn-scoped agent will therefore never prune unprompted, however
capable it is. That makes pruning a scheduled job rather than a disposition to
hope for — and a cheap, structural one, not an expensive introspective one.

## Reference

`docs/tasks/memory-index-split.md` (`index_only`, the description mechanism) ·
`docs/tasks/nightly-consolidation.md` (the nightly job doing three of the four
writes above; piece 3) · `docs/tasks/marlow-working-dir-git-policy.md` (why the
history is already in git).
