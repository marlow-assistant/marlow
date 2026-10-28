# Task — concurrent sends race on session continuity

**Status: SHIPPED 2026-08-03, then SUPERSEDED 2026-09-25 by `one-conversation.md`.** The server now owns the one conversation and resolves it when each turn runs, so the race below cannot happen: the phone no longer sends a session id, and the `canSend` gate and `_remap_pending_sessions` were deleted. Kept for the experiment, its results, and the designs rejected. The dead-`--resume` retry in `invoke.py` (§5) survives.

Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

Found in `/code-review medium` on the iOS cold-session-bug fix.

## 1. The bug

The iOS `ChatStore` tracked the conversation as one scalar, `currentSessionId`, read synchronously in `submit()` and written only once a polled `Turn` carried a `sessionId`, i.e. once that turn was terminal. The send button is never disabled while a turn is in flight, so a second message could be sent before the first resolved. Both `submit()` calls read the same `nil` and POSTed `/v1/message` with no `session_hint`. `WorkQueue.enqueue()` bakes `session_id` into the `Turn` at enqueue time and never re-resolves it, so strict-FIFO draining did not help: two independent sessions were created, and whichever poll resolved last won client-side, discarding the other thread. Reachable **only** while `currentSessionId` was `nil`, i.e. before the first turn of a conversation ever resolved.

## 2. Spike result — session ids are stable across `--resume`

Run in a clean shell 2026-08-03 (not nested inside a Claude Code session, which would have lied):

```
fresh=00000000-0000-4000-8000-000000000001
resume1=00000000-0000-4000-8000-000000000001
resume2=00000000-0000-4000-8000-000000000001
```

`--resume S` returns `S`. Three load-bearing consequences:

- **Once a conversation has a session id, the client's copy is permanently correct.** N rapid type-ahead sends all POST the same correct `session_hint` and FIFO drains them each `--resume S`. That is the behaviour Mike wanted, with zero changes.
- **The race has exactly one window: the first turn of a conversation** (once per install, and once more after each Unpair).
- **A second race the first design pass added machinery for isn't reachable.** `pollLoop` writing `currentSessionId` from any in-flight poll in whatever order they land would have let an older poll clobber a newer session with rotating ids. With stable ids every poll reports the same value, so the write is idempotent.

A second spike confirmed the failure mode for a resume target the CLI no longer has: `No conversation found with session ID: …`, exit 1. A real but separate wedge; see §5.

## 3. Constraint (from Mike, revised)

Original: real interactive Claude Code lets you type ahead, so the send button must **not** be disabled during a turn, and the fix must queue at the *server*, not defer the client's POST (deferring "works" but nothing appears in `queue_depth` until the first finishes, which doesn't match the mental model of what's visibly queued). Revised 2026-08-03: disabling during the **first** turn, when the client has no session id, is fine. That relaxation is what made the fix small; on turn one nothing else can be in the queue anyway.

## 4. What shipped — gate the send button on "session known or nothing in flight"

`ChatStore` exposed a published `canSend`, false exactly while a first turn was in flight and no session id known. The first pass keyed it on `activeTasks.isEmpty` and was wrong in both directions (it waited on tasks it had no business waiting on, and couldn't let go of a turn still nominally polling), so it became an explicit `gatingSends` set, released on any of three edges: the task drains; the `currentSessionId` write; or **the turn stalls** (fast phase expires; ~5 min at the time, so the gate was bounded by that in every case). A turn already labelled "still checking in the background" is not worth an hour of dead send button; if the cause is an unreachable server, a second send would fail on its own, and if the turn is genuinely slow, one forked thread beats an app that looks broken. Turns revived by `reconcilePendingMessages()` never gate: gating on them re-armed the block on every relaunch, so a stuck first turn survived a force-quit with Unpair as the only escape. No server change for the gate; cron is unaffected (it enqueues with no session and runs fresh).

**If the first-turn wait ever proved annoying** (a tool-heavy first message could hold the button for minutes), the softening was to accept the send and hold the POST locally with a visible "waiting for Marlow" state, not to reintroduce server-side session resolution. Never needed; `one-conversation.md` removed the problem instead.

## 5. A dead resume target wedges the conversation forever (survives)

When the CLI no longer has the session (its own history cleanup, default 30 days, or a wiped `~/.claude/projects`), `--resume S` exits 1 with `No conversation found with session ID: S`. That surfaced as `InvocationResult(is_error=True, result_text=None)`, so the client showed a generic "Something went wrong.", kept its permanently dead `currentSessionId`, and **every subsequent message errored identically**, with no path out but Unpair.

Fix in `invoke.py`: match that exact stderr signature (not merely `exit_code != 0`, which also covers auth failures we must not silently retry) and re-run once without `--resume`. The retried turn has no memory of the thread (unavoidable), but it answers, and the fresh `session_id` flows back through the normal poll so the client self-heals.

**The retry needed a companion in the queue, or it re-created design C's fork.** `enqueue()` bakes the id into every `Turn`, so a burst of type-ahead all carried the dead id: the head retried into a new session, and each turn behind it would fail, retry and land in a session of its own. The transcript looks continuous while Marlow forgets the message the user sent twenty seconds ago, once per queued turn. Losing a 30-day-old thread reads as expiry; losing the message just above it reads as broken, silently. `WorkQueue._remap_pending_sessions` (since deleted with the client-side session id) repointed every still-PENDING turn carrying the old id whenever `result.session_id` differed from the one asked for. Keyed on the mismatch rather than a "did I retry" flag, because §2 established `--resume S` returns S, so a mismatch *means* "the session asked for is not the session used".

## 6. Rejected designs, and why

Kept because they're the shape you'd re-derive without §2's spike result.

**A. Defer the second POST client-side** until the first send's session is known. Rejected under the original constraint (doesn't queue at the server); it became the softening named in §4.

**B. Server tracks last-session per device.** Regresses durability: `_turns` is in-memory, whereas the client's on-disk session id survives a server restart. Also folds cron turns into the iOS thread.

**C. Chain by predecessor turn** (`chain_from_turn_id`, resolved at drain time from `self._turns[ref].result.session_id`). Works, but a single failed turn forks the conversation permanently: turn *N+1* chains from a turn that produced no session, falls back to fresh and silently starts a new thread mid-conversation. Also needs the chain pointer persisted to survive a client force-quit, and its referent can vanish from `_turns` on a server restart.

**D. Client-minted `conversation_id`, server resolves the session lazily at drain time.** Strictly better than B and C (B's durability hole plugged, C's fork-on-failure self-healing) and the recommendation before the spike. Unnecessary then, because the client only doesn't know the session during turn one, which §3 let us wait out; it also carried a bonus (re-keying `get_identity_context`'s freeze cache to a stable id) which the spike dissolved, since session ids are stable and that cache already hit. This is, in effect, what `one-conversation.md` later built from the other direction: the server owning the conversation.
