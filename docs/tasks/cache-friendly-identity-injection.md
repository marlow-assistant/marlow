# Task — make identity/memory injection cache-friendly

**Status: MERGED 2026-07-31.** `identity.py`'s
`_session_cache` freezes the composed string per session. Corrected 2026-08-01;
this line still said "awaiting `/code-review high` + merge" after both had
happened. The rest of this doc is the spec it was built from.
**Priority (historical):** follow-up to build-queue #0
(the identity loader itself, `docs/tasks/loader-identity-injection.md`, is
already merged — this hardens it). **Shape:** small, targeted change to
`server/identity.py` + `server/invoke.py`; the empirical groundwork is already
done (gate1-results §8).

## Why this exists

`server/identity.py` currently reads `SOUL.md`, `USER.md`, and every file in
`memory/` fresh off disk on **every** `run_claude()` call, and the composed
string is passed via `--append-system-prompt` unconditionally on every turn
(`docs/tasks/loader-identity-injection.md`). That's correct behavior-wise
(gate1-results §8 confirms `--append-system-prompt` is never carried forward by
`--resume` — it must be resupplied every turn or identity silently drops out
mid-session). But it's not cache-aware, and the empirical finding is that this
matters a lot:

**Framing correction (2026-07-25): this task is insurance, not the fix.**
Freezing a 40K-token block is a workaround for the block being 40K. The durable
answer is to make it small — `docs/tasks/memory-index-split.md` — after which
mid-session churn costs noise. Do this one anyway (it's small, and its real
justification below is history invalidation, which survives the shrink), but
don't let it read as closing the problem.

- **`--append-system-prompt` caches as one opaque block.** Resupplying a
  byte-identical string turn-to-turn is nearly free (`cache_creation` collapses
  to near-zero, `cache_read` absorbs the rest). Changing **any part** of it —
  even a 22-character tail on a 16.5K-token block — forces the **entire block**
  to be recreated from scratch (full `cache_creation` cost again). There is no
  cache breakpoint between sections; ordering stable content before volatile
  content inside the single appended string buys nothing (confirmed directly —
  see gate1-results §8).
- **Today's loader re-reads `memory/` every call**, so any memory write that
  lands mid-session (which will happen routinely once Marlow is journaling
  during the day) silently makes every subsequent turn in that session pay
  full recreation cost — not a cheap diff, the *whole* SOUL+USER+MEMORY blob.
- At the current ~156KB of memory content (~35-40K tokens), a single mid-session
  memory write could turn a cheap cached turn into one that recreates ~40K
  tokens of cache, repeatedly, for the rest of that session's turns until the
  string happens to stabilize again.

This compounds with the separately-tracked index/retrieve split (memory
inject-all is already flagged as a stopgap in `identity.py`'s module docstring)
but is a distinct problem: even a small, already-split index will eat this same
tax if it's regenerated fresh on every call and happens to change mid-session.
The fix here is about **injection stability**, not content size.

**The real reason to do it (gate1-results §8a, 2026-07-25):** the block sits in
front of conversation history in the cache prefix. Changing it mid-session
therefore invalidates the block **plus all accumulated history** — and history
grows as the session does, so on a long attended conversation that's the larger
cost. This justification does not go away when the block gets small, which is
why the freeze is still worth building after the index split lands.

**Sizing — MEASURED 2026-07-25, and it came out in this task's favour
(gate1-results §8b).** The open worry was that cache entries might expire inside
a normal conversational gap, which would have made freezing near-worthless. They
don't: 90s and 240s gaps both cache-hit on a byte-identical 156KB block. Freezing
therefore pays across realistic gaps, and **this task keeps its priority ahead of
the index split** — the ordering question is settled.

Two things from that run to build against:
- **Design against a 240-second dependable window** — the longest gap actually
  measured to hit. Beyond it, retention looked opportunistic (a 420s gap missed
  while a 720s gap hit) and 300s was never sampled. Never plan around a hit
  past 240s; treat it as a bonus.
- **A miss costs ~58K tokens** at today's memory size, so the freeze is
  protecting something substantial — but note that the *same* run is what
  promoted `docs/tasks/memory-index-split.md`, because 58K tokens is ~29% of the
  context window occupied every turn whether it cache-hits or not. Both tasks
  are wanted; neither substitutes for the other.

## The core design decision — settle first (Plan step)

**Freeze the composed identity string for the life of a session; only
recompute it at a session boundary.** Concretely:

- On the **first turn of a new session** (no `session_id` / `resume=False`),
  build the identity context fresh from disk, as today.
- On **subsequent turns of the same session** (`resume=True` with a
  `session_id`), reuse the **exact same string** built at session start — do
  not re-read `SOUL.md`/`USER.md`/`memory/` again.
- A memory file written mid-session shows up starting the *next* session, not
  mid-turn. This is an acceptable staleness trade — v1's "read SOUL/USER/memory"
  was already this loose (discretionary, often stale within a conversation too),
  and the alternative (fresh-every-turn) is the expensive one.

**Why the freeze is safe, stated explicitly:** the obvious worry is Mike saying
"remember X", Marlow writing `memory/x.md`, and then two turns later Marlow not
knowing X because the frozen index predates the write. That doesn't happen —
gate1-results §8 confirms conversation history survives `--resume` independently
of the system prompt, so X is still right there in the transcript. **Within-session
recency lives in history; the injected block only has to carry what history
doesn't.** That is the same principle as §8a's general rule, and it's what makes
freezing a correctness-neutral change rather than a memory regression.

**As built:** `identity.py`'s `_session_cache`, in-memory, keyed by
`session_id`, pruned by `_prune_expired` with a max age (bounding staleness —
a session can technically live for days via `--continue`, so an unbounded
freeze would go arbitrarily stale). `get_identity_context(settings,
session_id=, resume=)` is the entry point. `SOUL.md`/`USER.md` aren't
special-cased — the freeze covers the whole composed string. A server restart
mid-session just costs one cache-miss turn, then stabilizes again.


## Reference

Empirical basis: `docs/marlow-v2-gate1-results.md` §8 (three clean-shell test
scripts run 2026-07-24 confirmed `--append-system-prompt` caching behavior —
opaque whole-block invalidation, no prefix reuse, resupplying identical content
is cheap).
