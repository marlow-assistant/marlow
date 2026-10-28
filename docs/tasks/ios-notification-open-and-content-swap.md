# Task — notification content and launch-time prefetch

**Status: SHIPPED.** Half A shipped 2026-08-10, Half B 2026-09-08, both
confirmed on Mike's phone. Two defects surfaced by using it were fixed
2026-09-10 and are described below. One thing is still owed, and two were closed unworked on
Mike's call — see the two sections of that name.

## What Mike reported

> I'm getting occasional notifications from Marlow — "Marlow has something for
> you" — and when I select them and the app loads, it doesn't actually refresh.
> Also, maybe same task, I'd like me looking at the notification to do the
> client-side notification swap (hit the server, replace the anonymous/limited
> "has something for you" with the actual message).

Half A was the stale transcript: `ChatView` now observes `scenePhase` and calls
`ChatStore.handleDidBecomeActive()`, which drains pending turns immediately
instead of waiting for the health tick
(`becomingActiveDrainsPendingTurnsWithoutWaitingForTheHealthTick`).

Half B was the banner, and then a second irritant Mike reported once it worked:
tapping a notification still took about a second to show text the banner had
already displayed.

## How it works, and the invariants behind it

**The privacy boundary is the point.** Real message content never transits
Apple's infrastructure. APNs carries only `{aps, type, ref}`; the phone fetches
the content itself over the tailnet, and a `UNNotificationServiceExtension`
rewrites the body locally. `mutable-content: 1` is permission to rewrite, not
content.

**`turn_ready` only.** `notify` already carries its own body and has nothing to
fetch. `permission_request` builds a second local notification with Allow/Deny
against the server's 55s `t_hook_seconds` deadline, and handing that path to an
extension that can stall on the network would spend the decision window before
the buttons appear. `tests/test_push.py` asserts presence on `turn_ready` and
absence on the other three, because the failure that matters is it spreading.

**The extension fetches `GET /v1/turn/{ref}` and must never call
`/v1/pending`.** `/v1/turn/{id}` reads a turn without touching
`Turn.delivered`; only `/v1/pending`, via `ready_undelivered_turns`, marks it
delivered. An extension calling `/v1/pending` would consume the turn before the
app durably recorded it.

**The generic alert is the fail-safe, not a loading state.** The original
content is copied before any work starts and returned unchanged on every
failure — bad `ref`, no credential, auth error, 404, non-terminal turn, decode
error, no route, timeout, system expiry. Never a blank body, never an error
string. `finish()` is the single exit and is called exactly once.

**The prefetch inbox is a cache, never the source of truth.** Because
`/v1/turn/{id}` does not mark delivery, the `/v1/pending` drain still runs on
its own schedule and delivery accounting is untouched. Every way the inbox can
fail — missing, corrupt, filed under the wrong id, non-terminal — degrades to a
poll, which is what every turn cost before it existed. `TurnPrefetchTests`
covers each degradation, because a cache that failed *closed* would be worse
than no cache.

**What the post-tap wait actually was.** The plan assumed a network round trip.
It was mostly a timer: tapping a banner lands on a `.running` placeholder,
`reconcilePendingMessages()` restarts a poll, and `pollLoop` sleeps its interval
*before* the first request — ~500ms plus a round trip to re-download text
already on screen. So `ingestPrefetchedTurns()` runs inside `init` **ahead of**
`reconcilePendingMessages()`: a placeholder resolved there never starts a poll
and never reaches the sleep. It also runs from `handleDidBecomeActive()`, the
warm-resume path, which is the common one.

**Resolving from the inbox must do everything a terminal poll does, not just
render.** Both halves of this were missing in the first version and both were
caught in review: cancel the poll still asking about that turn (left running it
re-appends the turn id, and ChatView plays a receive sound for a turn APNs
already sounded), and adopt `turn.sessionId` (skipped, the next message resumes
the wrong CLI session — `concurrent-send-session-chaining.md`). Both only bite
on the warm resume, because `init` resolves before any poll exists;
`aPrefetchedTurnDoesEverythingATerminalPollWouldDo` drives
`handleDidBecomeActive()` from inside a live poll for exactly that reason. A
version of that test built on `init` passes against both bugs.

**Exactly-once ingestion needed no new machinery.** `ingestUnsolicited` already
dedupes on `turnId` against the transcript, so a cron turn prefetched by the
extension and then re-offered by the drain is a no-op. The inbox feeds that
same function rather than a parallel path. `ios` turns resolve their
placeholder instead, since `ingestUnsolicited` deliberately skips that source.

## What the device runs proved

Neither Apple's nor Tailscale's documentation established that the VPN path is
available to a short-lived extension on a locked phone, so this was gated on a
physical-device spike before any of Half B was built. Answers, on Mike's phone,
2026-09-08:

- **Tailscale connected, app suspended, phone locked** — the extension
  authenticated, fetched the turn, and displayed the real text. Tailscale was
  already up, having just carried the message; a genuinely cold VPN path is
  therefore not separately established.
- **Tailscale disconnected mid-turn, phone locked** — the original generic body,
  as designed. The fail-safe holds on the device, not just by construction, and
  Tailscale did not silently reconnect underneath it.
- **Post-tap render, on the shipped build** — instant. "I certainly didn't
  notice a delay", against a wait he had reported as irritating. This is the
  only evidence that counts: the tests prove no poll *starts*, not that the
  frame renders full.

Do not re-litigate the reachability question on the strength of the
documentation being silent. It was silent before the run too, which is why the
run happened.

## Two defects found by living with it, 2026-09-09

Mike woke up to both. Neither was reachable by the tests that shipped, and the
reason is the same in both cases: the tests exercised one turn at a time, and
one turn at a time is exactly the case where both bugs are invisible.

**The transcript was in arrival order, not chronological order.** He unlocked
to the morning briefing, and *underneath* it the nightly consolidate that had
run hours earlier. `ingestUnsolicited` appended, and the two ways an
unsolicited turn reaches it do not arrive in the order the turns were made: the
extension prefetches each push into `TurnInbox` as it lands, so a turn whose
banner was seen is ingested at unlock, while a turn still unprefetched — the
consolidate, its banner swallowed by a sleep focus — only turns up later on the
`/v1/pending` drain. Two ingest paths on different schedules, one append.
`inbox()` is also a dictionary, so a night with several prefetched turns had no
defined order among them either.

It now inserts by `createdAt`, scanning from the end so the ordinary case (a
turn newer than everything on screen) stays one comparison on a transcript
`ios-transcript-scaling.md` expects to hold hundreds of rows. The insert is
stable and never re-sorts what is already there: a transcript an older build
wrote out of order stays as Mike saw it rather than rearranging under him.
`ChatView` was already safe — its `ForEach` keys on `message.id` and has a
comment anticipating exactly this — and the 30-minute timestamp dividers, which
had been reading as a lie, come right for free.

**Markdown reached the lock screen verbatim.** `alertBody` collapsed whitespace
and capped the length but never stripped markup, so a briefing arrived with its
`**` and `#` intact. `NotificationShared.plainText` now takes the markup off.

It is **deliberately not a markdown parser**. The failure modes are not
symmetric: a stray `**` on a banner is cosmetic, while eating the `_` out of
`turn_ready` or the `*` out of `2 * 3` would misreport what Marlow said, and do
it invisibly. So every rule is one that cannot fire on prose that was never
markup — CommonMark's flanking rules cut down to "an opener needs non-space
after it, a closer needs non-space before it", plus the intraword ban on `_`.
An unmatched opener is text and stays. Code inside a fence passes through
untouched, because `*` and `_` are literal there. Bullets become `\u{2022}`
rather than vanishing: collapsed onto one line without them, "the first the
second" reads as a single sentence and loses what the list was saying.

Both fixes are client-side. The server was right — `ready_undelivered_turns`
iterates `_turns` in insertion order, which is creation order.

## Still owed

- **The Keychain migration.** There are two live copies of the device token on
  the phone: `KeychainStore`'s private item, and `SharedCredential`'s copy in a
  group the extension can read. Migrating would be tidier, but that item is what
  "paired" means and getting the access group wrong un-pairs the phone. Mike
  weighed the duplicate against that risk and chose the duplicate. Revisit only
  with an upgrade test that proves the move on an already-paired device.

## Closed without being worked on, 2026-09-10

Mike's call, both of them. They are here so a later session does not re-open
them as oversights.

- **The force-quit case.** Never tested and now not going to be. Nothing should
  assume iOS invokes the extension after an explicit terminate; if it does not,
  the turns arrive on the `/v1/pending` drain instead — correctly ordered, since
  the fix above sorts by `createdAt` — and the only loss is the banner carrying
  real text. That is the fallback that was agreed when the feature was built, so
  the untested case degrades to the documented behaviour rather than to a
  defect. Do not spend a device run proving it.
- **Device evidence for the 300-character cap.** Unit-tested, and on 2026-09-10
  Mike reported the overnight notification "seemed correct" — which is a reply
  that looked right, not a reply observed being truncated at the boundary. Close
  enough: the cap only mis-fires by cutting a long banner in the wrong place,
  which is cosmetic and would be obvious the moment it happened. Re-open it on a
  banner that actually reads wrong, not on the absence of evidence.

## Two order-sensitive hazards

*`Marlow.entitlements`' `keychain-access-groups` array.* Adding the array at all
changes the default access group for items saved without an explicit
`kSecAttrAccessGroup` — which is how `KeychainStore.save()` writes the paired
token — to the array's **first** entry. The app-identifier group is listed
first, preserving exactly the previous behaviour. Reordering those two lines
un-pairs the phone on the next save. There is a comment saying so in the file.

*`SharedCredential.service` still contains the string `spike`.* It is the
address of an item already sitting in the Keychain on Mike's phone. Renaming it
strands a live bearer token that `clear()` can no longer reach. The name is
cosmetic; the orphan would not be.

## Building it on a device

1. `cd ios/Marlow && xcodegen generate`, then build to the phone from Xcode.
   **Automatic signing has to register two capabilities** — App Groups
   (`group.com.mikemason.Marlow`) and Keychain Sharing
   (`com.mikemason.Marlow.shared`) — on **both** the app and the
   `MarlowNotificationService` App IDs. If that fails it is a provisioning
   error, not a feature failure.
2. **Launch the app once before testing.** The extension reads a credential
   copy that only exists after `SessionStore` has run; a push before that
   records `no_credential`.

`scripts/pull-notification-events.sh` prints one line per extension run from
the attached phone: outcome, duration, characters displayed, never the text or
the token. (It was a Settings section until 2026-09-30.) It is kept because an
extension is otherwise invisible: another process, a locked phone, no console,
and every failure looks identical from outside. The log lives in the app
group's `Library/notification-events`, because `devicectl` can see nothing else
in an app group container.

Reading the outcomes: `ok` produced a body. `network_error` and `timeout` mean
no route. `no_credential` means step 2 was skipped — unless a
`mirror_failed_<OSStatus>` line is also present, which means Keychain Sharing
was never provisioned (`-34018` is `errSecMissingEntitlement`); the two have
opposite fixes. `http_401` is a shared-Keychain problem wearing a network
costume. `expired` means Apple killed the extension before its own 10s
deadline, which would mean the deadline is wrong rather than the route.

## Deleting the feature

The spike was promoted rather than thrown away, so this is no longer obvious
from the file names. `ios/Marlow/Shared/`,
`ios/Marlow/MarlowNotificationService/`, the `MarlowNotificationService` target
and both new entitlement blocks, `SessionStore.syncSharedCredential()`,
`ChatStore.ingestPrefetchedTurns()` and `resolvePlaceholder()` with the `inbox:`
init parameter, `scripts/pull-notification-events.sh`, `NotificationSharedTests.swift`
and `TurnPrefetchTests.swift` go together, then `xcodegen generate`. Keep the
`mutable-content` change and its tests — it is inert without an extension.

**Unpair once before deleting**, or the copied credential is orphaned in the
Keychain with nothing left to clear it.

## Not scoped

Whether the notification should carry a title distinguishing a cron turn from a
reply, and whether `docs/tasks/ios-app-badge.md`'s badge count changes. The
display-length question is settled: a conservative 300-character cap, no
summarization service.
