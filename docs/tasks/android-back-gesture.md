# Task — Android back closes the top layer

**Status:** parked 2026-11-06, not started. Web only: the native app has its own
navigation. Worth doing before invites (`pre-invite-polish.md`).

## The problem

The web app has no history handling, so Android's back gesture leaves the app
from anywhere. Nobody has reported it yet. It showed up in review of the Settings
panes, whose on-screen Back button makes people expect the gesture to work too,
but every sheet has the same gap. A new Android user would hit it within minutes,
and it looks like the app closing on them.

## What it should do

Back undoes the last thing that opened: a notice, then the list, then About, then
Settings, then the chat. At the chat it leaves the app as usual.

Do it for the whole app with one small shared mechanism, where each layer adds a
history entry as it opens. Settings alone would make back behave differently from
one screen to the next. The layers are Settings' pane stack, the to-dos and ideas
panels, the Marlow sheet and its pushed views, and voice mode.

## What to get right

- **Buttons and history stay in step.** Closing with Back, Done or × must not
  leave a stale entry, or the next gesture appears to do nothing.
- **Pairing.** `App.tsx` and `Pairing.tsx` use `history.replaceState` to strip the
  pairing link, and that has to keep working.
- **Voice mode.** Back takes the same path as Leave voice mode, so leaving
  mid-recording still sends nothing.
- **The approval card** resists dismissal on purpose, so back must not close it.
- **Settings' focus and scroll restore** must work when back comes from the
  gesture, not only the button.

## Testing

Playwright's `page.goBack()` covers the logic. An installed app and a Chrome tab
treat history differently, and only a real Android phone shows that, so it needs
a device check before it counts as done.
