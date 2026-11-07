# Task — Settings overhaul

**Status:** shipped on both clients 2026-11-06, tried on Mike's phone and box.
Part of `pre-invite-polish.md`. iOS `SettingsView` and `SettingsPanes.swift`;
web `web/src/app/Settings.tsx`.

## The problem

Settings has grown to eleven sections on web and eight on iOS, and the first
thing on either is server detail nobody changes. Everyday controls sit between
diagnostics, Unpair is on the same screen as them, and the two clients have
drifted (Reminders is its own section on web only; "Voice" against "Voice
mode"). Mike wanted few settings, but there are this many, so the job is to
organise them (Mike, 2026-11-06).

## The design: today's screen, with two rows moved down

An earlier three-pane draft moved too much (Mike, 2026-11-06: "too much of a
change from today"). The screen stays as it is, in one order on both clients,
except that server and build detail go behind two rows at the bottom.

**Settings:**

1. **Appearance and voice:** three rows with a leading icon, each showing the
   current choice and opening its picker: Avatar (the round face, replacing
   the heading "Marlow"), Text size (web only; iOS follows Dynamic Type, Mike,
   2026-11-06), Voice.
2. **Intelligence:** Low / Med / High and Model, as today. New footer: "Higher
   effort gives better answers, but can be slower to respond."
3. **Location:** as today.
4. **Your day:** as today. Web's separate Reminders section folds in here, as
   on iOS.
5. **Notifications** (web only), and only while it needs something. Hidden
   when push is on, since the row can do nothing then. Blocked or failed
   still shows, because that message is the only explanation.
6. **Updates:** as today.
7. Two rows with no heading: **Server connection** and **About**.

**Server connection:** Server, Connection, Device, then Unpair, with its
confirmation as now.

**About:** Build (App and Server, kept adjacent because the question is whether
they agree), then the open-source notices.

## Details

- **Words stay valid.** "Settings, under What's new", "under Your day" and
  "Check-ins" appear in `identity/`, `changes.json` and the What's New card, and
  all stay. Nothing there mentions Status, Build or Unpair.
- **Connection problems don't hide.** The chat already shows a banner for
  needing reauthorization or being paused, so the Connection row can move
  down. Confirm both clients' banners cover every state it shows.
- **Web needs a small pane stack.** The sheet has no navigation today: add a
  Back button, a heading for the pane, and focus moved to it on entry and back
  to the row on return. iOS already has `NavigationStack`.
- **Long values:** one line, truncated in the middle on iOS. On web keep the
  `@commit` end visible and clip the branch. Full text copies on tap, because
  these values get pasted into bug reports.
- **The icon footer moves into `FacePicker`,** where the choice is made. The
  note about tapping Done stays true.
- **Same labels on both:** "Voice", not "Voice mode". The Location footers
  differ because the clients do: web says "about a kilometre", which is what it
  sends (two decimals), and iOS says "your city", being Core Location's reduced
  accuracy.
- **Web's Avatar, Text size and Voice are panes too,** not the rows that opened
  in place, so a long voice list doesn't sit mid-screen. The row names only the
  voice; the list says what each is.
- **"Remind me again daily" becomes "Repeat reminders daily"** on both clients
  (Mike, 2026-11-06). Change `identity/CLAUDE.md` and the `changes.json` entry
  that quote it in the same PR, and the tests that find the switch by label.

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
