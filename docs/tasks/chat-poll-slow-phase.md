# Task — revisit the chat slow-poll phase (a *deletion* candidate)

**Status:** deliberately deferred, 2026-07-31. **Direction: removal, not
addition.** **Shape:** small, and negative — the likely outcome deletes code.

**Do not add engineering to this path.** Mike's call, 2026-07-31: *"I don't want
to add engineering to this path, basically."* If a future session finds a rough
edge here, the first question is whether the surrounding machinery should exist
at all, not how to harden it. Two known rough edges are listed at the bottom and
were left unfixed on purpose.

## What exists today

`ChatStore` polls a turn in two phases (`PollingPolicy`):

- **fast** — 150 attempts × 2s, ~5 minutes
- **slow** — 120 attempts × 30s, ~another hour

When the fast window closes without a terminal status, the message goes to
`.stalled` ("Marlow hasn't answered yet — still checking in the background") and
the slow phase carries on. Four pieces exist only to serve that second phase:

1. the slow phase itself in `pollTurn`
2. the `ChatMessage.Status.stalled` case
3. persisting `.stalled` (and therefore the `Codable` unknown-case fallback that
   protects a downgrade from wiping the transcript)
4. `reconcilePendingMessages` resuming `.stalled` across a relaunch

Plus the tests covering all of it.

## Why it's suspect

`.stalled` does not exist because the network is unreliable. It exists because
the **fast phase gives up at five minutes** while the message text promises that
checking continues. The slow phase is the machinery that keeps that promise
true. Remove the promise and the machinery goes with it.

The premise — that turns routinely run past five minutes — is an assumption
nobody has measured. If real turns finish well inside the fast window, the slow
phase is dead weight, and dead weight on a state machine is not free: it is four
things that can be wrong. The 2026-07-31 review found two real defects in
exactly this code (a `.stalled` row showing no spinner after a relaunch; a
downgrade wiping the whole transcript because a new enum case had no fallback).
Both were bugs in machinery serving an unvalidated case.

This is the same posture already taken elsewhere in the design — *optimize for
the reasonable case, accept the rare miss* (the cache-cadence decision,
`docs/tasks/usage-telemetry.md`). A turn that never finishes is the idle-miss
equivalent: real, rare, and probably not worth engineering against.

## What would settle it

Turn wall-clock duration, from real usage. **This data is already planned** —
`docs/tasks/usage-telemetry.md` phase 1 records "wall-clock duration of the
invocation" per invocation. No new instrumentation is needed; this question just
needs someone to look at the distribution once a week or two of real turns have
accumulated.

Decision rule, set in advance so it isn't relitigated:

- **Nothing crosses ~5 minutes** → delete the slow phase and all four pieces
  above. The fast phase alone covers reality; a turn that outlives it becomes a
  plain terminal error the user can resend.
- **A meaningful tail crosses it** (tool-heavy turns, web trawls) → keep it, and
  the machinery has earned its maintenance cost.

## What deletion would remove

`PollingPolicy.slowInterval`/`slowAttempts`, the second `pollLoop` call and the
interim `update` in `pollTurn`, `Status.stalled`, the `.stalled` arm of
`reconcilePendingMessages`, the `.stalled` branch in `MessageRow.pendingLabel`,
and the tests `aStalledMessageOnDiskResumesPolling`,
`aStalledMessageWithNoTurnIdCannotBeRecovered`, and
`aLateDoneAfterTheFastPhaseStillUpgradesTheMessage`.

Keep the `Codable` unknown-status fallback (`ChatMessage.Status.init(from:)`)
regardless — it protects against transcript loss on *any* future enum change,
not just this one, and `aStatusFromANewerBuildDoesNotWipeTheTranscript` is what
proves it.

**Also keep the test-suite deadlines** — `ChatStore.awaitPendingWork(timeout:)`,
`Gate.waitOpened(timeout:)`, and `awaitSettled`. Those are unrelated to this
question: they exist so a hung test fails diagnosably instead of running out a
30-minute macOS CI job at 10x billing (`docs/tasks/`… see
`.github/workflows/ios.yml` and the ios-ci-minutes constraint). They earned
their place the day they were written.

## Known rough edges, deliberately left

- **A `.stalled` message can restart its whole budget forever.**
  `reconcilePendingMessages` resumes it via `startPoll`, which re-enters
  `pollTurn` at the *fast* phase with a fresh ~65-minute budget. A user who
  relaunches during the slow phase resets the clock; a genuinely lost turn that
  fails transiently (500s, not 404s) can stay "still checking" across
  relaunches without ever reaching terminal `.error`. Arguably correct — a
  relaunch means the user is present and retrying is reasonable. Fixing it means
  persisting an attempt count, i.e. *more* machinery on the path we suspect
  shouldn't exist. Left alone.
- **`waitUntil`'s closure is no longer `@MainActor`.** Warning-only under
  `SWIFT_VERSION: "5.0"`; a hard error whenever the target moves to Swift 6
  language mode. Fix it as part of that migration, not before.

## Reference

`ios/Marlow/Marlow/Models/ChatStore.swift` (`PollingPolicy`, `pollTurn`,
`reconcilePendingMessages`); `docs/tasks/ios-minimal-client.md`;
`docs/tasks/usage-telemetry.md` (the duration data this depends on).
History (both 2026-07-31): the change that introduced `.stalled`, then the two
review fixes that prompted this note.
