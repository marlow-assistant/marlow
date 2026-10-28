# Don't web-push a device that's on screen

**Status:** built on `worktree-skip-push-on-screen`. Seen on Mike's iPhone
home-screen web app (2026-10-25): no banner or toast with the app open, and
a phone locked right after sending still got its banner, late as expected.
Not yet seen: a Mac Safari back tab still getting its pushes.

## The problem

iOS Safari has no exemption for a focused page: every push must show a
notification, or it counts against the site
(`web/src/lib/pushNotification.ts`, `mustShowEveryPush`). So with the web app
open, the service worker's focus check works but can only turn "nothing" into
a silent notification it closes at once. On an iPhone that still flashes a
blank "Marlow has something for you." banner over the reply you're watching
(Mike, 2026-10-25). The client can't stop it. The box has to not send the push.

## The signal: a request made *while on screen*

The obvious rule, "skip a device that made an authenticated request in the
last few seconds", is wrong for two of the clients that use Apple's push
service:

- **A background tab keeps polling.** The chat store's loops don't stop when
  the page hides (`chatStore.ts`; the health loop runs every 15 s). Mac Safari
  also pushes through `web.push.apple.com` and keeps a background tab
  running, so every push to a Mac with Marlow in a back tab would be dropped.
- **The service worker's own fetch is authenticated.** A push makes it fetch
  `/v1/turn/{ref}` for the banner text, while the page is hidden.

So the web client says when it's on screen. `api.ts` adds
`X-Marlow-Foreground: 1` to a request made while the page is visible *and*
focused, the same test the service worker uses (`c.focused &&
c.visibilityState === "visible"`). The service worker never sends it. The box
records the last time each device sent it, in memory only: after a restart
nothing is recorded, so every push is sent.

## The timing: hold, don't drop

How often an on-screen web client asks for something
(`chatStore.ts` `DEFAULT_POLICY`):

| state | requests |
|---|---|
| watching a reply, first 6 s | every 0.5 s (burst) |
| watching a reply, to ~5 min | every 2 s (fast) |
| anything else, app open | at least every 15 s (health loop) |

A phone that just went to sleep looks like one on screen. Its last on-screen
request was a second or two ago. Skipping on recency alone would lose that
push, which is the push that matters most. So the box doesn't decide from the
past. It waits for the next request:

- **No on-screen request in the last `RECENT` = 20 s:** send at once. On
  screen means a request at least every 15 s, so this device isn't. This is
  the usual case for a locked phone, because a reply rarely lands within 20 s
  of locking.
- **Otherwise, hold the push up to `HOLD` = 16 s,** the health loop's 15 s
  plus a second for the request itself. An on-screen request arriving after
  the push was raised means the page is up and will show the reply itself:
  drop the push. None by the deadline means the phone went away: send it.

The phone that locked just before its reply was ready therefore gets its
banner, at most 16 s late. A phone watching a reply sees no banner and no
delay, because its next 2 s poll arrives first. A hidden page, iOS asleep or
a Mac back tab, sends no on-screen requests, so its push is never held past
`HOLD` and never dropped.

## Scope

- **Apple endpoints only** (`web.push.apple.com`). Chrome and Firefox let a
  focused page's service worker show nothing, and that check sees the window
  directly, which beats any guess the box can make. There, holding would only
  add risk.
- **`turn_ready` and `notify` only.** These are the pushes a page on screen
  announces itself (`planFor` returns nothing when focused).
  `permission_request` keeps being sent at once: a hold of up to 16 s would
  come out of a 55 s decision window for the phone that just locked, and a
  late approval prompt costs more than a flash. The two reauth types are
  always shown, focused or not, so they're never held.
- **Not APNs.** The native app has no "show every push" rule. iOS hands a
  foreground app's alert to `willPresent`, and `APNsRegistrar` presents
  nothing there. There's no flash to fix, and holding would only delay the
  lock-screen push.

## Parked

- `permission_request` on an open Safari page still flashes. Revisit if it
  bothers anyone, perhaps with a shorter hold, since the client polls a
  running turn every 2 s.
- An on-screen page whose requests are all failing (tailnet down) looks
  asleep, so its push is sent. That's the safe direction.
- A request sent just before the phone locks can reach the box after the push
  was raised, so that push is dropped. The window is one round trip, about
  0.1 s on the tailnet. The reply is still in the chat, but there's no banner.
- The flash can still happen in two ways, both failing toward a banner, not
  a loss. The health loop sleeps 15 s *after* each pass, so on a slow link
  the gap between on-screen requests can exceed `HOLD`. And if the poll that
  saw the reply finish lands just before the push is raised, turn polling has
  already stopped and only the health loop is left. Lengthen `HOLD` only if
  this is seen, since a longer hold is a later banner for a phone just locked.
- A box restart during a hold drops that push, along with the task holding
  it. That's a 16 s window, only on a restart.
