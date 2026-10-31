# Re-pairing the iPhone app against a different box

Status: known bug, not started (found 2026-10-30, testing onboarding on the
trial VM).

## What happened
Mike unpaired the native iPhone app from his own box and paired it with a
fresh install. The app went on showing the old box's conversation, and the
new box's first-run greeting never appeared, so onboarding didn't start from
the phone. A browser paired to the same fresh box got the greeting and
onboarded normally, so the box was fine: the bug is in the client.

## Likely cause (unverified)
The app keeps its own copy of the conversation and the "greeting shown"
state, and pairing replaces the token and address without clearing them. An
unpair followed by a pair against another box then looks, to the app, like
the same conversation continuing.

## What done looks like
Pairing with a box that isn't the one the app was last paired with starts
clean: the local transcript, the greeting flag, cached memory/schedule views
and any per-box settings are cleared, and the new box's greeting shows. A
re-pair with the same box keeps history, as now. Check how the web client
handles it too (both clients), and decide what identifies "the same box" (the
server's address isn't enough if a box is reinstalled at the same name).
Test first: a client test that pairs box A, then box B, and expects no A
messages.
