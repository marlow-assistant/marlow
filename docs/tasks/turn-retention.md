# Task — turn retention

**Status: the resource-leak bug is FIXED.** `WorkQueue._turns` (`server/queue.py`)
now evicts terminal turns once the dict passes `MAX_RETAINED_TURNS = 256`,
oldest first, via `_evict_old_turns()` called at both points `_drain` reaches
a terminal status. PENDING/RUNNING turns are never evicted. No persistence —
just a count cap, because that is all the actual bug (unbounded growth in a
`Restart=always` process) needed.

**Revised after `/code-review medium` (2026-08-19) found two real gaps in the
first version of this fix:**

- **`_evict_old_turns()` ignored `delivered`.** `GET /v1/turn/{id}` — the
  normal fast-poll path a sending client uses — never sets `delivered`; only
  the rarer `/v1/pending` catch-up path does. So most turns stay
  `delivered=False` forever under ordinary use, and gating eviction on that
  flag would have disabled eviction almost entirely rather than protecting
  anything. Fixed instead with `EVICTION_GRACE_SECONDS = 300`: a terminal
  turn is never evicted until it's been finished for at least 5 minutes,
  comfortably longer than any normal poll gap. `Turn.finished_at` is the new
  field this floor is measured from.
- **`_client_msg_index`'s idempotency window was tied to the same count cap
  as `_turns`.** That's the exact risk the original (superseded) piece-4
  design flagged and wanted a short, separate window for. Fixed:
  `CLIENT_MSG_ID_WINDOW_SECONDS = 600` — a resend is only deduped within 10
  minutes of the original send, checked in `enqueue()` independent of whether
  the earlier turn is still in `_turns`. `_prune_client_msg_index()` ages out
  stale entries so the index dict doesn't grow forever either.

This started as `nightly-consolidation.md` piece 4, which framed the fix as
privacy-motivated ("a pile of personal content resident in memory
indefinitely") and proposed a ~48h retention window plus a separate short
idempotency window for `_client_msg_index`. **The privacy framing doesn't
hold:** the `claude` CLI's own session store already persists full
conversation content to disk, independent of `_turns` (`--resume`/`--continue`
rely on it — `docs/marlow-v2-gate1-results.md`:138). `_turns` is a
delivery/polling buffer, not the conversation record, and evicting it early
was never actually protecting anything an attacker with box access couldn't
already get from the CLI's own transcripts. So `_turns`' cap is sized purely
for the resource leak (a round, generous count), not for privacy — but the
original design's *other* instinct, that `_client_msg_index` needs its own
short window independent of `_turns`, turned out to still be correct; see
above.

## Open direction — not scoped, not started

Mike's observation (2026-08-19): most comparable products (Claude, other AI assistants,
Discord) persist message history server-side well enough that a new or
reinstalled client can sign in and read old messages. Today Marlow's server
holds none of that — "the record lives on the phone" was the deliberate v2
design (see `server/session_state.py`'s docstring), but it means a second
client (a hypothetical Desktop Marlow, a reinstalled app, a lost phone) has no
way to catch up.

If that's worth having, it's a real feature — an addressable, queryable
message-history store, probably built on top of what the CLI's session
transcripts already contain rather than duplicating them — not a tweak to how
long `_turns` keeps rows. It needs its own design pass: what "a conversation"
means as a queryable unit, pagination, which client(s) actually want it, and
how it relates to the CLI transcript that already has the content. Not sized,
not prioritized against the rest of `marlow-v2-START.md`'s list — this
section exists so the idea has a home, not to commit to building it.

## Known gaps this doesn't touch

Two things `nightly-consolidation.md` piece 4 also named, still open,
independent of the count-cap fix above:

- A turn held on `_reauth_needed()` is popped off the `asyncio.Queue` already;
  a restart while held orphans it as PENDING forever (`server/queue.py`,
  `_drain`'s first KNOWN GAP comment).
- Nothing age-limits a burst of turns released together after a long reauth
  gap — they all run back-to-back with stale context (`_drain`'s second KNOWN
  GAP comment).

## Reference

`docs/tasks/nightly-consolidation.md` (piece 4's original, superseded framing);
`docs/marlow-v2-gate1-results.md`:138 (CLI session store persists independent
of `_turns`); `server/session_state.py` (why session/rollover bookkeeping is
NOT in `_turns` either).
