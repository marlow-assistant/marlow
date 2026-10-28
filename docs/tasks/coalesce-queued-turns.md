# Task — coalesce queued turns into one invocation

**Status: shipped 2026-08-23. Squash-merged and deployed to the box at 19:00 MDT (801 pytest, 135 Swift at merge; merged on the `/code-review medium` review, although §7 had required `/code-review high` first). Measured on the box the same evening: it has never once fired, §5a. Read that before spending anything more here.**

Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

Asked for by Mike while reviewing `concurrent-send-session-chaining.md`: "if I queue multiple messages while Claude is answering a first query, do they result in multiple followup `claude -p` calls, or are concatenated into one? I think concatenation is likely right — this is me sending some train of thought type stuff, and it's better to get that to Marlow as soon as practicable." It builds on that doc's session remap (`_remap_pending_sessions`, called from `_drain` on every successful invocation) and shares the same corner of `queue.py`.

## 1. Why

One `Turn` was one `claude -p`: three type-ahead messages were three full invocations, serialized, each `--resume`-ing the one before. For train-of-thought input that is the wrong unit three ways:

- **Fragmented context.** Marlow answers "check my calendar" before "…and also the thing about Thursday" exists, then re-answers around it.
- **Latency.** Each invocation carries fixed overhead (identity injection, cache warm, model latency), strictly serialized.
- **Quota.** Three invocations against the Pro subscription where one would do.

## 2. The hard part is turn identity, not the drain

Coalescing at the drain is a handful of lines. The cost is client-side. Three messages are three `turn_id`s and the client polls three independent assistant placeholders; a coalesced invocation produces **one** result. Handing it to all three renders the same answer three times; handing it only to the last leaves two permanently empty bubbles. The saving grace: `ChatStore.send()` appends the user's bubble as its own already-`.done` message and only the assistant placeholder is tied to a turn, so the user's train of thought stays on screen as typed and only the absorbed placeholders need to disappear.

## 3. Design

A new terminal `TurnStatus.MERGED` plus `Turn.merged_into` naming the absorber. `_drain`, having popped a turn, pops further eligible turns (§4), concatenates their prompts and runs **one** `run_claude`. The **last** turn is the survivor and gets the result (so the answer appears at the bottom of the transcript, where the user is looking); earlier ones get `MERGED` and no result. Push stays as is: one `TURN_READY(survivor)`. Prompts join with a plain `\n\n`, no framing or labels: these are consecutive messages from one person, and scaffolding invites Marlow to treat them as a structured document rather than someone still talking. The client's `pollLoop` learns one case: `merged` removes that placeholder, persists, stops polling.

### 3a. What "terminal" costs

MERGED is a fourth terminal status, and three mechanisms built after the original design key off terminal-ness. A build forgetting any is subtly wrong rather than obviously broken:

- **`finished_at` must be stamped** where the merge is decided. `_evict_old_turns` uses it for the `EVICTION_GRACE_SECONDS` floor; a MERGED turn with `finished_at = None` is evictable the instant the cap is hit, removing the record the client still has to poll. `_drain`'s existing stamp is in a `finally` on the invocation path, which absorbed turns never enter.
- **`activities` must be cleared**, following the `cancelled` branch, so "terminal implies no activity" is true of every terminal path by inspection.
- **`ready_undelivered_turns` stays as is.** It filters to `DONE`/`ERROR`, so `/v1/pending` (the push-wake path) never surfaces a MERGED turn; the client learns about the merge through `/v1/turn/{id}`.

### 3b. Deploy the client first

**Not an additive protocol change, and the one sequencing trap.** `activities` could ship server-first because the iOS `Turn` ignores unknown keys. A new *status value* is different: `ChatStore.pollLoop` switches on `turn.status` with `default: continue // pending/running`. An old client meeting `"merged"` treats it as still running, burns its whole poll budget on a turn that will never change, leaves the placeholder stalled, and once eviction takes the turn gets a 404 and shows *"Marlow lost track of this message (the server may have restarted). Try sending it again."* That is exactly the "messages being lost" reading §7 wants to avoid, in the worst words. So the client's `merged` case ships and is on Mike's phone **before** the server half is deployed. Both can merge in either order; the deploy is ordered. (Build server first so the client has something real to encode against; deploy client first so no phone meets an unknown status. Both are true.)

## 4. What may coalesce

A run must be contiguous in queue order, and every turn in it must be eligible. **Restated 2026-08-23 as an allowlist, not a match-list.** The original three match rules were written when `Turn` had six fields; it had fourteen, four of the newcomers (`require_session`, `attachment_path`, `retire_session_on_success`, `raw_prompt`) each carrying a per-turn contract a shared invocation would quietly break. Matching on named fields makes every future field silently opt-*in* to merging. Inverted:

**Only `source == "ios"` turns coalesce, and only when every one of them has no attachments, is not cancelled, and has the default value for every other flag.** Anything else stops the run. That subsumes the original rules:

- **`source`**: never fold a cron turn into an iOS thread (cron enqueues hintless and runs fresh; merging would give it context it must not have and put its output in the user's chat). Also excludes compaction turns (`raw_prompt=True`, deliberately undelivered).
- **`session_id`**, evaluated *after* `_remap_pending_sessions` has run for the head turn, otherwise a burst that arrived against a since-replaced session id looks ineligible when it is the exact case this helps most.
- **`resume`**: a fresh turn mixed into a resumed run silently acquires history it was enqueued not to have.
- **Attachments** (`attachment_paths` since multi-image, 2026-10-23; any non-empty tuple blocks): `run_claude` takes one turn's attachments per invocation, and cleanup runs on the invocation path only, so an absorbed turn's image would be orphaned. A turn carrying an image never absorbs and is never absorbed. Mike sends one image at a time; this costs nothing.
- **`require_session` / `retire_session_on_success`** are cron-only by construction; named so the exclusion is a stated fact rather than a coincidence a future attended feature could break. Same for **`cancelled`**.

Stop the run at the first non-matching turn rather than scanning past: FIFO is load-bearing. **Cap the run** at 5 turns so a pathological backlog (the overnight-cron burst the reauth-pause comment worries about) can't become one enormous prompt; the number is a guess, any bound is correct. Further turns come off with `get_nowait()`, not `get()`, so an empty queue ends the run rather than blocking the drain. **The pop is irreversible** (`asyncio.Queue` has no un-get, and peeking `_queue._queue` is not acceptable), so a popped-but-ineligible turn is held in a single instance carry slot consumed before the next `await get()`; every popped turn still gets its `task_done()`, and **`depth` counts the carry slot**, exactly as it counts the reauth-held turn, or `/health` shows `queue_depth: 0` with real work waiting.

Review follow-up: the server classifies every `Turn` dataclass field (invocation payload, compatibility key, eligibility contract, lifecycle metadata). A future unclassified field disables coalescing and fails a focused test, instead of silently opting its per-turn contract into a shared invocation.

## 5. Honest limit

The head turn has already started when the rest arrive, so a three-message train gets an answer to #1 with partial context, then a combined answer to #2+#3. Closing that fully would mean holding the first message briefly to see if more follow, which is worse: Marlow deliberately idle while the user types, and `concurrent-send-session-chaining.md` §3 already rejected the client-side version. Don't.

### 5a. Measured on the box 2026-08-23 — the window is smaller than the feature needs

First evening deployed: seven attended messages in bursts, **zero merges.** Gaps between consecutive `invoking claude -p` lines in `journalctl -u marlow-server`:

| gap | seconds |
| --- | --- |
| 19:00:43 → 19:00:53 | 10.1 |
| 19:00:53 → 19:00:58 | 4.6 |
| 19:00:58 → 19:01:02 | 4.7 |
| 19:01:02 → 19:01:57 | 54.4 |
| 19:01:57 → 19:02:08 | 11.2 |
| 19:02:08 → 19:02:12 | 4.2 |

**Turns complete in 4-11 seconds**, and that is the whole story. The design assumed a merge harvests everything sent "while Marlow was busy". It does not: a merge needs **two** messages sitting in the queue at the *instant* an invocation returns. Once the next turn pops and starts, a third message is already too late and pops alone. Two phone messages cannot be typed into a 4-second window, and the single 54s turn had only one message behind it.

Nothing is broken. Both halves are deployed; the fail-closed classifier was checked live against the running code (19/19 fields classified, so `_can_coalesce` is not short-circuiting); eligibility is satisfied on every one of these turns. There is simply no window at Marlow's current speed. The value is narrower than predicted (the live activity labels were thought to make the user type more, not less): this fires on genuinely slow tool-using turns and essentially never otherwise.

**Do not respond by adding a hold-and-wait timer.** §5 rejects it on grounds the measurement does not touch. The trigger for revisiting is Marlow getting *slower* (heavier tool use, higher effort), not any change here.

## 6. Interaction with the first-turn send gate

None: `canSend` only engages on the first turn of a conversation; coalescing only applies once a session exists to resume.

## 7. Decisions

- **A merged placeholder vanishes silently. Decided by Mike 2026-08-03**, no "answered together" marker. Rejected: collapsing a run's placeholders into one labelled marker, more literally truthful but transcript machinery to narrate something the user already knows, having just typed all of it. Revisit only if it reads as messages being *lost* rather than answered together; the user's own bubbles all stay on screen, which should make it read correctly.
- **Merged turns must not count as unread** (added 2026-08-23, the day the app badge shipped). The `merged` case does not append to `completedAssistantTurns`, does not badge, does not play the receive sound: one run is one arrival, not three. Handled before session adoption, so an absorbed turn retaining a retired hint cannot race the survivor and overwrite its fresh session id.
- **First client code to remove a `ChatMessage` from `messages`.** `activeTasks` and `gatingSends` are keyed by `placeholderId` and reconciliation looks up by `clientMsgId`; returning `true` from the `merged` case runs the existing terminal teardown rather than a parallel one. Relaunch reconciliation resumes through `pollTurn` and needs no case of its own.
- Both halves went to one session, one branch, one PR (Mike, 2026-08-23), a deliberate exception to the then rule against UI work for a second implementor: the client change is a state transition, not layout. What no test can see, and Mike must check on the phone after deploying in §3b's order: two or three quick messages while Marlow answers, earlier assistant bubbles disappear cleanly, the user's bubbles remain, one reply lands at the bottom, exactly one receive sound.
- `/code-review high` was required before merge (it touches `server/queue.py`, on the CLI-contract path); it merged on the medium review.
- Run the iOS suite with `-derivedDataPath` pointed at a scratch dir: a shared DerivedData run once silently dropped a whole suite.

## 10. Open — a failed coalesced run retries only the last message

Found in review 2026-08-23, **deliberately not fixed**: it needs a coalesced run *and* an invocation error to reach, and the fix is a design change rather than a repair.

**The shape.** If `run_claude` raises or returns `is_error` on a run of three, the survivor goes ERROR and the two absorbed turns are already MERGED, their placeholders removed. The phone shows the three user bubbles and one red error, which reads correctly as "the whole burst failed". But Retry is keyed to the *survivor's* `clientMsgId` (`ChatStore.retry`), so it resends only the third message. The first two were never delivered, have no bubble left, and nothing separates "answered" from "never sent": the user gets a reply addressing a third of what they asked. The error is honest; the recovery is quietly partial.

**Decision rule.** Leave it until a coalesced ERROR is actually observed on the box; the `coalesced N turns into ...` log line is the only trace a merge leaves. If one shows up, **defer the MERGED stamp to the end of the run**: keep absorbed turns PENDING while the invocation is in flight, then stamp MERGED on success or ERROR (with the survivor's error) on failure, giving every message its own red bubble and Retry through the existing error path, no iOS change. That contradicts §3a's "stamp `finished_at` where the merge is decided" (right for today's build) and changes the success path: placeholders would spin until the answer lands and then collapse together, instead of disappearing one by one. That is a UX call, and Mike's, which is the other reason it is parked.

**Status 2026-08-23:** no coalesced run has happened at all (§5a), so this failure mode has never been reachable. Watch for `coalesced N turns into ...` in the journal first.
