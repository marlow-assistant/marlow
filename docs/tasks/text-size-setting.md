# Task — in-app text size setting

**Status:** not started (2026-10-15). Raised by the Android user, who finds phone text
hard to read early in the morning and asked for a desktop app. The cheaper answer is the installed
Chrome PWA (see the install note below) plus this setting. **Scope both
clients up front; it is not done until both have it** (root `CLAUDE.md`).

## Why a setting, when zoom exists

Browser zoom (Ctrl +) already works on desktop and is remembered per site, so
for the Android user's PC this is mostly discoverability. The real gap is elsewhere:

- **Web sizes text in `px`.** `web/src/app/styles.css` and `views.css` carry
  about 50 `font-size: NNpx` declarations. Android Chrome scales only
  `rem`/`em`/`%` with the system font-size setting, so Android
  friends who have set "Font size: large" get no change from Marlow today.
- **iOS already follows Dynamic Type.** The views use semantic styles
  (`.subheadline`, `.footnote`, `.body`), so Settings > Display & Brightness >
  Text Size works. There is nothing broken on iOS. The only question is
  whether an in-app control is worth having there at all.

## Decisions (Mike to confirm)

1. **Web first, and it is the real work.** Convert the `px` font sizes to
   `rem` and drive the root size from one setting. That fixes the Android
   system-size gap and gives the in-app control with the same change. Do not
   use CSS `zoom` on the app root: it fights browser zoom and the sticky
   composer's geometry tests.
2. **Three or four steps, not a slider.** Default / Large / Larger / Largest
   (roughly 100, 115, 130, 150%). Layout has to survive the top step, so the
   Playwright geometry tests should run at it.
3. **iOS: `.dynamicTypeSize` override at the app root**, same steps, stored in
   `UserDefaults`. It is a few lines, and it keeps the two clients' Settings
   screens matching. If Mike would rather rely on the OS setting, drop this
   half and write "iOS: follows Dynamic Type, no in-app control" in the Status
   line. That is a legitimate answer, not a gap.
4. **Storage:** web in `localStorage` is fine (a per-device preference; the
   service worker never reads it, unlike push state in `lib/storage.ts`).
   Per-device, never synced through the server.
5. **No server change, so no box deploy.** Facts-on-server, wording-on-phone
   applies: this is purely client.

## What to watch

- **The px to rem sweep is where bugs hide.** Fixed-height rows, the composer,
  the approval card and the activity list all assume sizes. Check each at the
  largest step. The `markdown.css` sizes need the same treatment.
- **Icons and tap targets** should not shrink at Default and should grow
  sensibly at the top step.
- **Identity:** nothing changes in the harness affordances, so no
  `identity/CLAUDE.md` edit.

## Done means

- Web: sizes in `rem`, a Settings control, persisted, Vitest for the
  step-to-size mapping and the stored default, Playwright geometry checks at
  the top step (composer, message rows, approval card). Verify the
  verification: revert the rem conversion and watch a test fail.
- iOS: the root override and Settings row, or the explicit decision to follow
  the OS setting only. If built, run `xcodegen generate` for any new file.
- Observed on a real Android phone with the system font size raised (never
  seen yet), and by the Android user on their PC.

## Related: the install suggestion for the Android user

Before any native desktop shell: install Marlow from Chrome on Windows as an
app (taskbar icon, own window, web push toasts, Chrome's "continue running
background apps" for the tray-ish behaviour). Edge is unsupported
(`portable-web-client.md`). The one thing a PWA cannot do is a global hotkey;
if they still want it after living with the installed app, that is the trigger
for a thin Tauri wrapper (also gives Mike Marlow-on-Mac). Not built, and it
should wait for that felt symptom.
