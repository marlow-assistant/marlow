# Task — red badge on the app icon for pending messages from Marlow

**Status:** DONE 2026-08-23, as a binary indicator. Retired to a record of what
shipped; the pre-build design survey is in git history if the reasoning is ever
needed again (it was `git log docs/tasks/ios-app-badge.md`, before this commit).

## What Mike asked for

A red indicator (the standard iOS unread badge) on the Marlow app icon, so a
message from Marlow is visible without opening the app or having a notification
banner still on screen.

## What shipped

**Binary, not a count.** Always `1` when there is anything unread. An exact
count needs real per-device read state, and nothing has that — the server
tracks delivered/undelivered per turn (`WorkQueue`), which goes true as soon as
`/v1/pending` hands a turn over, whether or not a human has looked at it.
Inventing a number from that would be a number that means nothing.

**The client is authoritative; the payload is the fallback.** The design
question was "server or client", and the answer is both, in that order of
trust:

- `APNsRegistrar.handleRemoteNotification` sets the badge itself when a
  `turn_ready` push wakes the app while it is inactive. The app is the only
  party that knows the chat has not been opened. The rule lives in
  `marksChatUnread(forUserInfoType:appIsActive:)` — pure and tested, beside
  `presentationOptions`, which it deliberately mirrors.
- `server/push.py`'s `dispatch()` also sets `aps["badge"] = 1` on `turn_ready`.
  This is what covers a wake that never happens at all: background execution
  denied, or iOS simply declining to launch the app.

**Only a ready turn counts.** Reauthorization and permission pushes have their
own surfaces; badging the chat for them would make the badge mean nothing in
particular. A failed send doesn't badge either — it is already visible in the
transcript with a Retry affordance, and the user was holding the phone when it
happened.

**Cleared when the chat is genuinely on screen** — `scenePhase == .active`, in
`ChatView`. Not merely when the view appears: see below, it is the whole bug.

## The bug worth remembering

Shipped server-side-only first, and the icon stayed clean every single time
while the banner arrived normally. `ChatView` was clearing the badge from an
unguarded `.onAppear`. A `turn_ready` push carries `content-available`, so iOS
can launch a terminated app **straight into the background** to handle it — and
SwiftUI runs `onAppear` for that background scene exactly as it would for a
visible one. The badge APNs had applied moments earlier was wiped by a launch
the user never saw.

Two lessons, both cheap: `onAppear` is not "the user is looking at this", and a
feature whose whole job is to be visible while the app is *not* running cannot
be verified by any test in this repo. This one took three device passes.

## Still not scoped

Exact unread counts; multi-device behaviour (badging and clearing across more
than one paired device inherits the open question in
`multi-device-permission-routing.md`).
