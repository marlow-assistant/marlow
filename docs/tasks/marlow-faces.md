# Task — choose Marlow's face

**Status:** shipped on both clients (merged 2026-10-16), live
on Mike's box and iPhone. Still open, device checks: web on a real Android
phone (picker, sheet, greeting, pairing, a notification), and on iOS a sheet
closed by swipe (the icon catches up on the next foreground) and a Done that
iOS refuses mid-transition (never seen).

## What it is

Eleven portraits (male, female and androgynous; monocle or glasses; a range
of skin tones). Face 1 is the original Marlow, brightened 2026-10-16, and the
default. Settings > Marlow > Appearance, or the face atop the Marlow sheet,
opens the picker.

- **One face everywhere.** The box holds the choice in `/v1/preferences`
  (`server/preferences.py`), with the Thinking level; clients keep a local copy
  and sync it (iOS `PreferenceSync`, web `lib/preferenceSync.ts`).
- **iOS home-screen icon** follows the face (alternate icons; `project.yml`
  needs both ASSETCATALOG settings, see its comment). It changes on Done, not
  per tap, since every change raises iOS's alert; a face taken up from another
  client changes it at once.
- **Web home-screen icon** stays face 1: a manifest icon is fixed at install.
  Notifications show the chosen face (`chosenFaceIcon`); the small app icon
  beside them is the browser's or the installed app's and can't change.

## Three framings per face

| framing | where | defined in |
|---|---|---|
| icon | iOS home screen | `scripts/face-icons.sh` (cuts the PNGs) |
| header | 52pt avatar, Settings row, Marlow sheet, web notifications | `MarlowFace.swift` and `web/src/lib/faces.ts` |
| large | picker tiles, empty-chat greeting, pairing | same two files |

Each is a zoom plus an anchor, the same model everywhere. The two client tables
must stay in step by hand. Every large crop is currently zoom 1 (Mike
unzoomed them all); kept as its own framing until it has been lived with.

## Adding or re-cutting a face

The 1254px originals stay out of git (Mike keeps them in
`~/Documents/MarlowFaces-2026-10-16`; `marlow-1.png` is also
`marlow-icon-square.png` at the repo root, and `marlow-1-noir.png` is the
original before brightening).

1. App images: a 768px JPEG imageset (`MarlowFace<N>`) and a 512px
   `web/public/faces/marlow-<N>.jpg`.
2. Icon: an `AppIcon<N>` appiconset, its name in `project.yml`'s alternate
   icon list, a row in `face-icons.sh`, then run it.
3. Crops: a row in both client tables. Tune by eye; the face crop tuner used
   on 2026-10-16 emulates all three framings.
4. Notification icons: `scripts/face-notification-icons.sh` (reads
   `faces.ts`; rerun after any header-crop change).

`MarlowFaceTests` checks every face has an image and that the built plist's
alternate icons match the table; `faces.test.ts` checks the web files exist.
