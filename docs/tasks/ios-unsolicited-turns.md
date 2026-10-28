# iOS — unsolicited turns never render

**Status:** found 2026-08-07 on the first real `morning` cron run, on device.
**Fixed the same evening**, on branch `ios-tz-icon-ux`. **Merged to `main`
(2026-08-08)**, after `/code-review low` caught a second bug in the fix
itself — see the new bullet under "Three decisions worth keeping" below.
Acceptance test passed on-device once; cron job files remain deliberately
unwritten pending more days of reliability.

Blocked stage 4 while it stood: the whole value of the morning job is a message
arriving that Mike did not ask for, and the client could not display one.

## Correction to the first diagnosis

The original write-up (below, kept because the reasoning about `/v1/pending`
being consuming still governs the fix) said the reply had been **fetched and
dropped**, and was therefore unrecoverable. **That was wrong**, and the actual
situation was better: `getPending` existed on `APIClient` but was never on the
`ChatAPI` protocol and **nothing in `ChatStore` ever called it**. So the cron
turn was never fetched at all — it sat in `ready_undelivered_turns()`,
undelivered, and turn `8abe2444` was still there hours later.

Worth stating plainly because it changes what the bug *was*: not "the client
mishandled a delivered turn" but "the client never asked". The consuming-endpoint
hazard is real and the fix is built around it, but it had not yet fired.

## What happened

`cron/entrypoint.py morning` → turn `8abe2444` → 39s → reply produced → APNs
push delivered. Mike got **"Marlow has something for you"** on his physical
phone. Tapping it opened the app to **no message**, and without scrolling to the
bottom.

So every layer works except the last one. The reply exists; the client has no
way to show it.

## Why

`ChatStore` is built around the *request/response* shape it was specified for:
`send()` appends a `userMessage` and a `placeholder` locally, and every path
that later renders a reply resolves it **against that local placeholder** —
`startPoll(turnId:placeholderId:)`, and the reconciliation loop in
`resumeInterruptedWork()` which iterates `messages` (the local array) and polls
only turns it already knows the id of.

A cron turn has no local placeholder, because the phone never sent anything.
There is no code path that ingests a turn the client did not originate.

**The dangerous part is `GET /v1/pending`, which is consuming.**
`ready_undelivered_turns()` marks the turns it returns as delivered as a side
effect (this is documented in `ios-minimal-client.md` and is why
`/v1/permissions` exists as the non-consuming sibling). So the sequence is:

1. cron turn completes, sits undelivered;
2. the app polls `/v1/pending`, the server hands the turn over and marks it
   delivered;
3. the client drops it on the floor for want of a placeholder;
4. the reply is now gone from the server's undelivered list.

It survives only in the CLI transcript (`marlow-last-turn.sh` on the box prints
it). **A silent morning briefing is not merely missing — it is unrecoverable
through the API.**

## What shipped

`ChatStore.drainUnsolicitedTurns()` on the existing 15s health tick (its own
call, not a branch inside `refreshHealth` — a `/health` failure must not also
skip the drain), feeding `ingestUnsolicited(_:)`. Plus `getPending` added to the
`ChatAPI` protocol, which is what had made it uncallable.

Three decisions worth keeping:

- **The discriminator is `source != "ios"`, not "no local placeholder."** A turn
  this device sent is the send path's business from POST to terminal, and
  `/v1/pending` returns it too once it finishes. Keying on the missing
  placeholder would double-render a fast turn in the window between the server
  finishing it and `startSubmit` recording the id it got back. Source is
  unambiguous from the first byte. Pinned by `anIosTurnIsLeftToTheSendPath`.
- **`currentSessionId` is deliberately untouched.** Pinned by
  `aCronTurnDoesNotBecomeTheSessionTheNextMessageResumes`.
- **`persist()` runs immediately on append**, not at the next natural save,
  because between fetching and writing this process holds the only copy.
- **`drainUnsolicitedTurns` does *not* check `Task.isCancelled` after the
  fetch**, and is the only awaiting path in `ChatStore` that doesn't. Review of
  the fix caught the first version doing it by reflex, copied from
  `refreshHealth`. The guard is right there and wrong here: `refreshHealth`
  protects the UI from stale state, but a `/v1/pending` response in hand has
  *already* cost the server's `delivered` flag, so returning early destroys the
  briefing — with no id recorded to re-fetch it by. Pinned by
  `aTurnAlreadyHandedOverIsKeptEvenIfTheStoreIsCancelled`.

The regression test for the actual bug is
`theHealthTickFetchesUnsolicitedTurnsWithoutBeingAsked`, and it is the one that
matters: the six tests written first all drove `ingestUnsolicited` directly and
would have passed against the broken build, because the ingestion logic was
never what was broken — the *wire* was.

Also fixed alongside, since Mike hit it in the same breath: `ChatView` now
scrolls to the newest message `.onAppear`, not only on `messages.count` change.
A cold open from a notification restores the transcript from disk without
changing the count, so the one moment the newest message matters most was the
one it wasn't shown.

One latent inaccuracy found on the way: `Turn.fixture` hardcoded
`source: "device"`, which was never a value the server emits (`server/app.py`
enqueues `"ios"`). Harmless while nothing read the field; actively misleading
now that ingestion keys on it.

## What the fix had to do

Ingest turns the client did not originate: for a turn arriving from
`/v1/pending` with no matching local placeholder, append a new assistant message
rather than discarding it.

Three things to get right rather than discover later:

- **Idempotency.** `/v1/pending` is consuming, so a turn is handed over exactly
  once, but the client persists its transcript and reconciles on launch — an
  ingested turn must not be appended twice across a relaunch. Key on `turn_id`.
- **Do not fold it into the session.** A cron turn runs with
  `session_id=None, resume=False` — a fresh CLI session by construction. It is
  not a reply in Mike's conversation thread and must not repoint the stored
  session hint, or the next thing he types resumes the *cron* session. See
  `concurrent-send-session-chaining.md` for the remap machinery this has to stay
  clear of.
- **Scroll to bottom on open from a notification.** Mike hit this in the same
  breath ("without auto-scrolling to the bottom"). Related to but distinct from
  `ios-chat-ux-gaps.md` §3, which is about the scroll anchor jumping when a
  permission row clears; this one is about where the view lands on cold open.

## Worth reconsidering while in here

`/v1/pending` being consuming is what turns a rendering bug into data loss. It
was the right call for the request/response flow it was designed against — a
turn is delivered once and the client owns it thereafter. Unsolicited turns
break that assumption, because the client can now fail to own something it was
handed. Either the client must never drop a turn, or delivery needs an
acknowledgement rather than being implicit in the read. The first is much
cheaper and is what the fix above assumes; note the second exists if this bites
again.
