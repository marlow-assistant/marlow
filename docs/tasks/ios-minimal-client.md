# Task — minimal iOS client (gate #6, reduced scope)

**Status: ✅ all six steps done.** Steps 1–4 (pairing, push registration, chat
screen) shipped 2026-07-29/31. Steps 5–6 (push handling, permission approval)
shipped later as their own tasks, on-device tested: `apns-push-delivery.md` and
`ios-permission-round-trip-ui.md`. The acceptance test (a cron job fires
unprompted and its result arrives as a notification, phone locked) has been
happening for real every morning since the crontab went live 2026-08-08
(`v2-dogfood-parity.md`).

Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

Project is generated via `xcodegen` (`ios/Marlow/project.yml` is the source of
truth). Bundle id `com.mikemason.Marlow` is `MARLOW_APNS_TOPIC`.

**This was deliberately not gate #6 as planned.** Plan §7 step 6 specified chat
UI + push + on-device whisper.cpp STT + permission approval. **STT, macOS and
streaming were cut**: they made gate #6 read as weeks of work and none is on the
path to "Marlow tells Mike about his email at 07:00". Streaming needs SSE on the
server as well as UI work (the perceived-latency design, START known gap #2,
still stands).

## Why an app at all, rather than a web page or Discord

Considered and rejected (2026-07-29):

- **A browser client served over the tailnet** is the cheapest thing that works,
  but a polled web page cannot nudge. Marlow's value is unprompted delivery; a
  surface Mike has to open first is not a surface.
- **A Discord harness** would deliver notifications reliably today with no Apple
  track (and is why v1 worked). But every email scan and answer would transit
  Discord in plaintext, inverting `push.py`'s contract that content never
  touches third-party infrastructure. Surfaces are sticky; a stopgap would become
  permanent.

Both Apple blockers were already cleared (Developer enrollment Active, both APNs
keys exist); a paid team means a normal 1-year provisioning profile, no 7-day
re-signing treadmill.

## Design decisions

**Wake-and-fetch, then raise a *local* notification.** The APNs push carries
`{type, ref}` and a fixed generic body only. On receipt the app fetches the real
content over the tailnet and posts a local notification containing it. Apple sees
nothing but an opaque reference. This is the mechanism that makes the iOS route
worth its cost over Discord; don't shortcut it by putting text in the payload.

**The code lives in `ios/` in this repo.** No personal content (identity lives in
the separate private repo), versioned alongside the API it talks to, publishable
if the repo is ever open-sourced.

**Base URL is always the MagicDNS name, never a `100.x` address.** The TLS cert is
a real Let's Encrypt-backed `tailscale cert` issued for the name, so **no App
Transport Security exception is needed**; if you find yourself adding one,
something else is wrong.

**Send `timezone` on every message** (`TimeZone.current.identifier`). Optional, so
an older build keeps working and the server falls back to the box's own zone. But
the box is pinned to `America/Edmonton` and Mike travels, so without it every
briefing is in the wrong hour the moment he flies. On the message rather than a
register-once call for the same reason: a zone captured at pairing is wrong after
the first flight. An unresolvable value is logged and dropped, never a 4xx: a bad
zone string must not be why a message fails to send. Client half shipped
2026-08-07.

**`GET /v1/pending` was consuming** (the endpoint was removed 2026-10-01;
`docs/chat-ux-contract.md`): it marks the turns it returns as delivered and
they won't come back from that endpoint. Persist what it returns on receipt; it is
not a re-readable inbox (specific turns stay fetchable by id without consuming
delivery status). `/v1/permissions` exists as the non-consuming sibling
(`ios-unsolicited-turns.md`).

**Pairing codes are issued by Mike over SSH** (`POST /internal/admin/pairing-code`
on loopback) and typed into the app; there is no in-app way to get one, by design.

**Out of scope, still:** any auto-retry of a permission decision, or any local
mirror of `boundaries.py`. The server decides; the app asks.

## What shipped, briefly

SwiftUI, iOS 17+, single target, no external dependencies. Keychain (not
`UserDefaults`) for the device token, a bearer credential for a server that
approves tool calls for a shell-capable agent. `POST /v1/device/apns-token` fires
on every launch, since tokens rotate on reinstall/restore/OS update. `ChatStore`
(send → poll → persist, `/health` polled every 15s for reauth/paused banners) and
`ChatView` are the chat screen.

`/code-review medium` on the first chat-client change found and fixed four
`ChatStore` lifecycle bugs same-day: a leaked poll Task past Unpair,
permanent-vs-transient error handling, no reconciliation after poll timeout, a
stuck placeholder across a force-quit. Each was re-introduced as a deliberate
mutation and confirmed to break a test; three seams enable that (a narrow
`ChatAPI` protocol, an injectable `persistURL`, an injectable `PollingPolicy`).
The suite uses Swift Testing rather than XCTest for its async ergonomics around
cancellation and late completion. CI gates it in a separate workflow from
`ci.yml` so the slow macOS job only fires on `ios/**` changes (CI is off now; the
local suite is the gate).

**Send + keyboard dictation (fixed 2026-08-11).** Sending while Apple's
*keyboard* mic was still running sent the turn but left the text in the field. A
live keyboard dictation session holds text that hasn't reached the SwiftUI binding
yet and commits it a beat after anything the app writes, so clearing `draft`
inline was undone. `submitDraft()` now drops focus first (ending the session,
forcing the commit) and reads `draft` a run-loop turn later, which sends the
trailing words instead of eating them. **Anyone touching this:** the same window
means `draft` can lag what's visibly in the field mid-dictation, so the Send
button's `draft.isEmpty` disable check can't be trusted as "nothing to send"
during dictation. This is unrelated to `DictationController`, which never writes
to `draft`. A send refused by the first-turn `canSend` gate restores the text
*and* raises `DictationController.reportSendBlocked()`, worded neutrally about how
the text was entered.

**Open question on this client:** `chat-poll-slow-phase.md`. The two-phase poll
and its `.stalled` state may be machinery for a case that never happens: a
*deletion* candidate; don't add engineering to that path.
