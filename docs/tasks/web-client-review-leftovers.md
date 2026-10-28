# Task — web client review leftovers

**Status:** parked 2026-10-02, deliberately not built. Items 1–3 are findings from the
local `/code-review high` on the portable web client's PR, each real and
each declined for now on cost/benefit. The same review's findings 1–3 (body
read outside the timeout, push "failed" lost on relaunch, the sticky
permission notification) were fixed on that branch. Each item below has its own
trigger; none is filler to self-assign. (A fifth item, the What's new face
overlapping its title, was fixed 2026-10-24: the face had no round clipping
parent, which `FaceImage` relies on.)

## 1. One browser can hold push under two device tokens

`PairingStore.set_webpush_subscription` (`server/pairing.py`) stores a
subscription against one token and never looks for the same `endpoint` under
another. Normally harmless: unpairing and a 401 both call `unsubscribePush`
(`web/src/app/push.ts`), so the browser re-pairs with a fresh endpoint and the
old one answers 410 and is pruned.

`unsubscribePush` swallows its errors, though. If it fails and the browser
pairs again, `getSubscription()` returns the same subscription with the same
VAPID key and `syncPushSubscription` registers it under the new token as well.
The old token's endpoint is live, so it never 410s and is never pruned: that
browser gets every push twice, permanently.

**Fix when built:** in `set_webpush_subscription`, drop `webpush` from any
other entry with the same `endpoint` (one write, under the same save).
**Build it when:** anyone reports duplicate notifications from the web client.

## 2. The notifications card nags a user who blocked notifications

`NotificationsPrompt` (WEB-9) comes back on every launch while push is not on,
including `denied`, where it offers only "Not now". A user who blocked
notifications on purpose (a desktop used only while the tab is open) sees the
card on every launch, with no way to dismiss it for good short of granting the
permission.

WEB-9 chose "back each launch" deliberately, for the case it was built for:
a friend's Android install where notifications were simply never turned on.
The `denied` case was not the one under discussion.

**Fix when built:** a "Don't ask again" on the `denied` (and maybe `failed`)
card, kept in IndexedDB's kv store; Settings still shows the state.
**Build it when:** Mike or a friend finds the card annoying in use.

## 3. Every message bubble is a Tab stop

`Copyable` (`web/src/app/CopyMenu.tsx`, RND-4) puts `tabIndex={0}` on each
bubble so the context-menu key can reach the Copy menu. With no windowing
(WIN-2), Tab from the header to the composer walks every rendered row:
hundreds after "Show earlier".

**Fix when built:** a roving tabindex on the transcript (one stop, arrow keys
between rows), or drop keyboard Copy and leave the browser's own selection.
**Build it when:** someone uses the desktop client from the keyboard and hits it.


## 4. A visible but unfocused window gets both sound and toast

The service worker shows no toast only for a window that is focused *and*
visible (`web/src/sw.ts`); the page plays its reply and approval sounds on
`visible` alone (`App.tsx` `isForeground`, `PermissionCard.tsx`). A window on
screen behind another app gets both. Found while chasing
`web-double-notification.md`, which turned out to be a stale pairing; never
observed here.

**Don't** fix it by making the page silent when unfocused (a one-attended-rule change for page sound and the push toast, closed unmerged): a
user with push off or delayed then gets *no* alert for an approval that times
out to a deny. **Fix when built:** make the worker suppress the toast whenever
the window is visible, focused or not, and leave the page's sound alone.
**Build it when:** someone hears sound and toast together with the window
visible but not focused.
