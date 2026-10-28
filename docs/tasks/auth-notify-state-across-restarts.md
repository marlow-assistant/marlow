# Task — persist AuthWatcher's notify state across restarts

**Status:** parked 2026-08-18, deliberately not built. Found in `/code-review
medium` on the branch that added `AuthWatcher`, reported as a real
finding and then declined on cost/benefit — this file is the decision, not a
backlog item waiting for a free afternoon.

## The defect

`server/auth_monitor.AuthWatcher` keeps `_notified_phase` and `_notified_at`
in memory only. Every process restart constructs a fresh watcher with
`_notified_phase = None`, so the first poll after the restart takes the
`phase != self._notified_phase` branch and pushes again, regardless of how
recently the last notification went out.

Concretely: the token enters its 24h pre-warn window and a push goes out. The
server is then restarted three times during a deploy inside that window. Mike
gets four notifications instead of one, and `auth_notify_repeat_hours` — the
whole throttle — is bypassed each time.

That is the same class of noise the `AuthWatcher` change set out to remove, which is why it was
worth writing down rather than shrugging at.

## Why it is parked

- **The failure direction is safe.** The bug produces *extra* warnings about a
  real deadline, never a missed one. The thing this machinery exists to
  prevent is silence.
- **The frequency is unmeasured, and probably tiny.** The window is 24h out of
  a ~9-day token life, so a restart has to land inside roughly 11% of the
  calendar to duplicate anything at all. The box is a long-lived systemd
  service; restarts are deploys, not routine.
- **The fix is not free.** It means a new on-disk state file (or a new field in
  `session_state.json`), its read/write/corruption handling, and its tests —
  real machinery against an edge case nobody has hit.

## Decision rule — build it when either is true

1. Mike actually receives duplicate auth pushes and says so. One report is
   enough; this needs no threshold.
2. The box starts restarting on a schedule or in a crash loop for any other
   reason — at that point the 11%-of-calendar argument above stops holding.

## If it is built

Persist `(phase, notified_at)` next to the existing state in
`server/session_state.py` rather than inventing a second store, and load it in
`AuthWatcher.__init__`. Treat an unreadable or absent file as "never
notified" — failing open to one extra push is correct here, and matches
`queue.py`'s existing fail-open reasoning for `_reauth_needed()`.

## See also

- `server/auth_monitor.py` — `AuthWatcher.check()` is where the state lives.
- `docs/tasks/apns-push-delivery.md` — push delivery semantics.
