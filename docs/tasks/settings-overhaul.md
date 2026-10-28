# Task — Settings overhaul

**Status:** not started (2026-10-21). Raised by Mike while testing voice mode:
"some stuff is weird" on the Settings screen, and it needs an acknowledgements
page for the Apache-2.0 code the app now ships. Both clients: iOS
`SettingsView` and web `web/src/app/Settings.tsx` (a port of it) have the
same sections and the same problems.

## What Mike flagged

- **A section called "Marlow" inside Marlow's settings.** It holds only
  Appearance (the face picker, which also changes the Home Screen icon). The
  whole screen is Marlow's settings, so the heading says nothing.
- **The server URL wraps onto a second line** (Status → Server), and so do
  the build labels at the bottom (Build → App `branch@commit`, Server).
  That wastes vertical space on a screen that's already long.
- **No acknowledgements.** Voice mode's Phonon voice runs on FluidAudio,
  compiled into the app under Apache-2.0. Apache-2.0 §4 requires a copy of
  the licence (and any NOTICE text) to go with a binary distribution. The
  Phonon model itself is downloaded, not shipped, but crediting it costs
  nothing. Nothing in the app does this today. That's fine on Mike's own
  phones, but needed before any wider release.

## Review the whole screen, not just those

Mike wants the screen reviewed end to end. Current order (iOS,
`SettingsView.swift`):

1. **Status:** Server, Connection, Device (last 6 of the token).
2. **Marlow:** Appearance (`FacePicker`); its footer explains the icon change.
3. **Intelligence:** Thinking (effort picker), Model; footer on effort and cost.
4. **Location:** sharing level; footer.
5. **Voice mode:** Phonon voice, download / Play a sample / Delete; one-line
   footer linking the model card (`VoiceSettingsSection`).
6. **Build:** App `branch@commit`, Server build.
7. **Unpair** (destructive, with a confirmation).

Questions to settle with Mike before building:

- **Order.** What is used most (Appearance, Thinking, Voice) arguably
  belongs above diagnostics (Status, Build), which are for him and for
  support. One option: put diagnostics in an "About" page with the build
  lines and the acknowledgements.
- **Headings.** "Marlow" goes. Appearance could join a general section, or
  stand alone as a row.
- **Long values.** Truncate in the middle (`.truncationMode(.middle)`,
  single line) or shorten: the host without the scheme, `branch@commit`
  with the branch clipped. Full text can sit behind a tap, or a long-press
  to copy.
- **Footers.** Several are a paragraph. Voice mode's was cut to one line
  (Mike, 2026-10-21: "way too much information"); apply the same bar
  everywhere.
- **Web parity.** The web port follows the same order and copy; change both
  in one PR.

## Acknowledgements page

- A row at the bottom of Settings, or in the About page above, opening a
  plain scrolling list: FluidAudio (Apache-2.0, full licence text), Phonon
  31m by Gradium (Apache-2.0, linked), plus anything else compiled in.
  Audit `Package.resolved` when building it. sherpa-onnx was removed on
  2026-10-21, so it doesn't need an entry.
- Keep the texts as bundled resources, not typed into Swift. A test that
  every resolved Swift package has an entry would catch the next
  dependency added without one.
- Web: same list, if the web client ships any of these (today it doesn't
  ship FluidAudio; check its npm dependencies' licences instead).

## Not in scope

- New settings. This is layout, wording and the licence page only.
