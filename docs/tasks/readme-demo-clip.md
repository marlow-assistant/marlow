# Task — a demo clip of the transparency pane for the README

**Status:** parked 2026-10-28, maybe. The README's still screenshots do the
job for now; this is a nice-to-have that may never be built.

## Why

The README's Activity screenshot shows one tab of the pane behind the
avatar. A short clip stepping through all of them (Activity, Usage,
Schedule, Memory, You, Soul) and opening a memory file would show how much
a user can inspect better than any still.

## How, if it happens

- Simulator, dark mode, the `-uiFixture idle` launch argument
  (`ios/Marlow/Marlow/DebugFixtures/UIFixture.swift`), whose content is
  generic on purpose. Status bar overridden to 9:41 and a full battery
  (`xcrun simctl status_bar`).
- `xcrun simctl io <udid> recordVideo`, then an MP4 and, if it reads well, a
  GIF under about 5 MB (ffmpeg with a palette). 10–15 seconds.
- Taps land late in this app: leave a second or two between them.
- **Keep Settings out of it.** Under a fixture only the chat's store is fake,
  so Settings shows the install's real server address and device ID.

The cost is upkeep: any change to those panes makes the clip stale, and
redoing it is a recording session, not a screenshot.
