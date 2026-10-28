# Task — real APNs delivery (`push.py`) + APNs token storage

**Status: BUILT and exercised in production.** Real APNs delivery landed 2026-07-29;
bounded latency/hangs, the sibling `.p8` key and env validation followed 2026-07-31. All
three wake paths wired: `PERMISSION_REQUEST` (tested on-device 2026-08-03/04,
see `ios-permission-round-trip-ui.md`), `REAUTH_NEEDED`, and `TURN_READY`
(the morning cron pushes have been landing on the phone daily since the crontab
went live 2026-08-08 — see `v2-dogfood-parity.md`). 15 tests in
`tests/test_push.py`.

## The design decision, still load-bearing for anyone touching this

**The push payload stays opaque: `{type, ref}`, never content.** This is why
v2 targets iOS rather than reviving the Discord surface — Gmail/calendar/
message text must never transit Apple's infrastructure. Never put a turn's
result text in the APNs body; the app fetches content over the tailnet and
raises a **local** notification with it instead
(`docs/tasks/ios-notification-open-and-content-swap.md`).

**Every wake sends an alert push with a generic body, never a pure silent
push.** A `content-available`-only silent push is throttled by iOS at its
discretion and unreliable in Low Power Mode — unacceptable for a 07:00 nudge.
Send both: a visible alert with a fixed, content-free body ("Marlow has
something for you") *and* `content-available: 1`.

`dispatch()` never raises into its callers (a dead APNs connection must not
fail a permission round-trip or kill the queue's drain loop) and no-ops
without raising when no APNs config is present (local dev/CI have no `.p8`).
A 410 Unregistered response clears that device's stored token. `REAUTH_NEEDED`
fires once on entering the pause, not on every recheck poll.

## Config

| env | meaning |
| --- | --- |
| `MARLOW_APNS_KEY_PATH` | path to the `.p8` (default `~/.marlow/apns/`) |
| `MARLOW_APNS_KEY_ID` | `<prod key id>` / `<sandbox key id>` |
| `MARLOW_APNS_TEAM_ID` | `<team id>` |
| `MARLOW_APNS_TOPIC` | the app's bundle id |
| `MARLOW_APNS_ENVIRONMENT` | `sandbox` \| `production` |

Separate keys per environment (Apple's recommendation), so key id and
environment are independent settings. `.p8` files live at `~/.marlow/apns/`
(`700`/`600`), never committed, same posture as `.credentials.json`.

## Reference

Plan §10.3, §4 step 3, §6b. Consumer:
`docs/tasks/ios-minimal-client.md`. Source of the proactive turns:
`docs/tasks/scheduled-work.md`. Full original build spec (implementation
walkthrough, test list) is in git history if ever needed.
