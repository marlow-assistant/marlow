# Task — Settings overhaul

**Status:** designed 2026-11-06 with Mike, not built. Part of
`pre-invite-polish.md`. Both clients in one PR: iOS `SettingsView` and web
`web/src/app/Settings.tsx`.

## The problem

Settings has grown to eleven sections on web and eight on iOS, and the first
thing on either is server detail nobody changes. Everyday controls sit between
diagnostics, Unpair is on the same screen as them, and the two clients have
drifted (Reminders is its own section on web only; "Voice" against "Voice
mode"). Mike wanted few settings, but there are this many, so the job is to
organise them (Mike, 2026-11-06).

## The design: a main pane and two below it

The test for the main pane: would someone change this in normal use? Anything
that describes the install rather than how Marlow behaves moves down a level.

**Settings** (the main pane, same order on both clients):

1. **Look and sound:** Appearance, Text size (web only), Voice. Each is a row
   showing the current choice, opening its picker one level down.
2. **Intelligence:** Thinking, Model. Model stays read-only, next to Thinking,
   so the two can be read together.
3. **Location:** the sharing level.
4. **Your day:** Starts, Ends, Check-ins, Remind me again daily. Web's separate
   Reminders section folds in here, as on iOS.
5. **Notifications** (web only): iOS gets this from the OS.
6. Two rows with no heading: **What's new** and **About**.

**About:**

- **Connection:** Server, Status, Device.
- **Build:** App, Server, kept adjacent because the question is whether they
  agree.
- **Acknowledgements**, one level further down.
- **Unpair**, last, with its confirmation as now.

**What's new:** "Show after updates" at the top, then the list. Its own pane
rather than a section of About: it is written for the user and the update card
points at it. Web's in-place expanding list goes.

Rejected: Updates inside About (it would bury the one thing here meant for
everyone, under server detail). Tabs or a search box: too much for about
fifteen controls.

## Details

- **Words stay valid.** "Settings, under What's new", "under Your day", "Remind
  me again daily" and "Check-ins" all appear in `identity/`, `changes.json` and
  the What's New card, so these names and their top-level place must not
  change. Nothing there mentions Status, Build or Unpair.
- **Connection problems don't hide in About.** The chat already shows a banner
  for needing reauthorization or being paused, so Status can move down. Confirm
  both clients' banners cover every state the Status row shows.
- **Web needs a small pane stack.** The sheet has no navigation today: add a
  Back button, a heading for the pane, and focus moved to it on entry and back
  to the row on return. iOS already has `NavigationStack`.
- **Long values:** one line, truncated in the middle on iOS. On web keep the
  `@commit` end visible and clip the branch. Full text copies on tap, because
  these values get pasted into bug reports.
- **The icon footer moves into `FacePicker`,** where the choice is made. The
  note about tapping Done stays true.
- **Same labels on both:** "Voice", not "Voice mode"; the same Location wording.

## Footers

One line at most; a permission problem still replaces the Location line.

- Intelligence: "Higher is slower, not more expensive."
- Location: "Sent with each message. Approximate shares your area, Precise your
  exact position."
- Your day: "Marlow checks your inbox during your day and tidies up after it."
  and, with the switch, "Unfinished reminders come back a day later."
- What's new, About: none.

## Acknowledgements page

- A row in About, opening a plain scrolling list: FluidAudio (Apache-2.0, full licence text), Phonon
  31m by Gradium (Apache-2.0, linked), plus anything else compiled in.
  FluidAudio links more than its own code: NeMo text processing (NVIDIA,
  Apache-2.0) and Rust crates under MIT or Apache-2.0, listed in
  `dependency-licenses.md` § iOS. Audit `Package.resolved` when building it. sherpa-onnx was removed on
  2026-10-21, so it doesn't need an entry.
- Keep the texts as bundled resources, not typed into Swift. A test that
  every resolved Swift package has an entry would catch the next
  dependency added without one.
- Web: its own list from the production npm dependencies, which
  `dependency-licenses.md` § npm records. It ships no FluidAudio, but it does
  ship `jsqr` (Apache-2.0) and `phonon-tts` (MIT OR Apache-2.0).

## Not in scope

- New settings. This is layout, wording and the licence page only.
