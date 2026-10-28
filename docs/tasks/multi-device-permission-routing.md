# Task — route permission requests to the "right" device instead of all of them

**Status:** placeholder, opened 2026-08-04. **Priority:** future feature, not
scoped. **Shape:** unknown until designed; likely touches
`server/permissions.py`, `server/push.py`, and the pairing/device-token model
in `server/pairing.py`.

## What prompted this

Came up in the security review of `ios-permission-round-trip-ui.md`:
that change's server-side authorization is correct as built (any paired device's
token can list/answer any pending permission request — by design, matching
`GET /v1/pending`'s existing behavior), but the *product* consequence is that
if Marlow is paired to two devices, both are pushed and both show the same
request. Mike already lives with this in v1 — permission requests currently
show up both as a Discord message (Accept/Deny buttons) and in the Claude
Code surface on his phone — and calls it "approval spam": annoying when one
device is clearly the live one and the other is dormant.

## Open questions for the design session

- What signal indicates a device is "live" vs dormant — most recent
  `getTurn`/poll activity, foreground state, something explicit like a
  "primary device" toggle?
- Route (push only the chosen device) vs broadcast-but-suppress (push all,
  but have answering on one device cancel/dismiss the notification on
  others)? The latter is more resilient if the "live" signal is wrong.
- Does this generalize across surfaces (Discord + iOS today, maybe more
  later) or is it iOS-device-specific?
- Interaction with the existing single-use `resolved` guard in
  `server/permissions.py` — first answer wins already; this is purely about
  who gets *asked*, not a new authorization concern.

## Not in scope here

This doc is a placeholder to make sure the idea doesn't get lost, not a spec.
Don't implement anything from the open questions above without a real design
pass.

## Cheap precursor — let a device carry a human label (added 2026-08-07)

**Priority: low, but it keeps costing time.** Mike's ask, and the third or
fourth time it has come up: a device in the pairing store should be markable —
at minimum "this one is the simulator" — so the logs say which device they mean.

Today `server/push.py` logs an eight-character prefix of an opaque token:

```
INFO [server.push] no apns_token registered for device=XXXXXXXX...; skipping push
```

That line is *correct* — the iOS Simulator cannot obtain an APNs token, so
skipping is the only right answer — but it is indistinguishable from the real
failure it describes, which is a physical phone that never registered. It cost
a session-worth of wrong conclusion on 2026-08-07: the line was read as "push
has never worked", the parity plan's APNs delivery assumption was called a
blocker, and it took Mike knowing which device was which to unwind it. A log a
reader can misread this confidently is a defect in the log, not in the reader.

Two pieces, roughly:

- A label on the pairing entry (`pairing.py` already stores a dict per device
  token, so this is a field, not a schema). Set at pairing time from the
  client's own name, or by hand — `XXXXXXXX` predates any of this.
- `push.py` logging the label and, when the device is known to be a simulator,
  saying so: `device is the simulator; skipping push` rather than the
  `no apns_token registered` line, which is a different claim.

Do **not** infer simulator-ness from "has no APNs token" — that is the
conflation this is meant to remove, and it would relabel a genuinely broken
phone as expected-and-fine, which is the one failure this must not hide. It has
to be something the device or Mike asserts.

Filed here rather than in `apns-push-delivery.md` because the underlying want is
the same one this doc exists for: the server cannot currently say anything about
*which* device it is talking to, and both routing and this need it to.

## The same question for every push, not just approvals (added 2026-10-02)

Mike, dogfooding the web client on his desktop: while chatting live
there, his phone "blows up with notifications". Every `turn_ready` and
`permission_request` is broadcast to every paired device (`push.broadcast`),
so the device he is watching *and* the one in his pocket both fire. Open
question, not scheduled.

What the server already knows, for free:
- **Which device token is polling.** A client watching a turn calls
  `GET /v1/turn/{id}` every 0.5–2s and `GET /v1/transcript` every 15s, all with
  its bearer token. "This token fetched this turn within the last few seconds"
  is a strong, cheap signal that a live client already has the reply.
- **Not foreground state.** A backgrounded browser tab keeps polling at a
  throttled rate, and an iOS app suspended mid-turn stops. Polling is evidence
  of attention, not proof of it.

Shape of a fix (a sketch, not a decision):
- **Suppress the other devices only on strong evidence** that one client was
  live for *that turn*: it polled the turn id within the last few seconds, and
  the client says it is visible (the web client already knows; a header or a
  field on the poll would carry it). Weak or missing evidence means broadcast
  as today.
- **The asymmetry is the design constraint:** a duplicate notification is
  annoying; a missed one is the failure the push exists to prevent. Every
  ambiguity has to resolve toward notifying.
- **Approval prompts may want the opposite answer.** Pushing a
  `permission_request` to the device in the pocket while the desktop shows the
  modal is exactly "approval spam", but if the desktop user has walked away,
  the phone is the only path to an answer within the 55s.
- The web client already avoids double-announcing on *its own* device (a
  focused tab shows no banner and plays its own sound; see
  `web/src/lib/pushNotification.ts`). This is the cross-device half.
