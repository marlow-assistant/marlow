# Task — tap a sent photo to see it full screen

**Status:** not started. Specced 2026-10-24 for an implementor session. Ready
to build on `main`: the multi-image bubble grid it builds on merged
2026-10-24.

## Why

A photo in a chat bubble is a 220pt thumbnail, and in a multi-image message it
is a square-cropped tile. Once a photo is sent, nothing in either client shows
it any larger, and you can't save or copy it (Mike, 2026-10-24, testing photo sending
on his iPhone). This adds a tap to open it full screen, with save and copy.

## Scope: both clients, or it is not done

iOS (`ios/Marlow`) **and** web (`web/`, which is the Android client). Do not
write "shipped" for one of them.

## Behaviour (both clients)

1. **Tap opens.** Tapping a loaded image in a **user** bubble opens a
   full-screen viewer. This covers the single-image bubble and any tile of a
   multi-image grid. Grey placeholder tiles (images not yet downloaded) do
   nothing when tapped.
2. **Whole image, not the crop.** The viewer shows the full image fitted to the
   screen on a black background. The bubble's square crop is display only.
3. **Swipe between the message's images**, starting at the one tapped, in the
   message's image order. With one image there is nothing to swipe.
4. **Zoom.** Pinch to zoom on iOS, and double-tap toggles between fit and 2x.
   On web, pinch-zoom on a touch screen is enough: don't block the browser's
   own pinch, and don't build a custom zoom.
5. **Save or copy.**
   - iOS: a `ShareLink` with the current image as `Image`/`Transferable`. The
     share sheet gives Save Image and Copy for free.
   - Web: a download button (an `<a download="marlow-photo.jpg">` on the blob
     URL). The browser's own long-press menu on Android, with Save and Copy
     image, must still work on the `<img>`, so put no
     `pointer-events: none` or overlay on top of it.
6. **Close:**
   - iOS: a close button (top-leading, 44pt target) and swipe down.
   - Web: a close button, Escape, and the browser Back gesture. Push a history
     entry when the viewer opens, and pop it on close.
7. **Accessibility:** "Photo 2 of 5" style labels; the close and save
   controls are labelled.

The image is the stored copy, not the original: the on-device downscale has a
long edge of 1568px at JPEG quality 0.6 (`LONG_EDGE` in
`web/src/lib/imageDownscale.ts`, `ImageDownscaler` on iOS). Don't add any
upload or storage of originals: that is out of scope.

## Where the code is

### iOS

- `ios/Marlow/Marlow/Views/ChatView.swift`:
  - `MessageRow.imageGrid` is the bubble's image view, for 1 image or a grid.
    Each loaded `Image(uiImage:)` there gets `.onTapGesture` opening the
    viewer at that index.
  - Slots come from `message.allAttachmentIds` (server rows) or
    `message.allImageFilenames` (own sends). Load a slot with the `loadImage`
    closure that `MessageRow` already receives, which is
    `store.loadedImage(filename:)` and is cached.
- The bubble also has `.contextMenu { copyMenu }` (long press). **Keep it
  working.** Tap opens the viewer and long press still shows the copy menu.
  Check this one in the simulator: a tap gesture plus a contextMenu on the
  same view is where it can break.
- **New file** `ios/Marlow/Marlow/Views/ImageViewer.swift`, holding a
  `struct ImageViewer: View` that takes `images: [UIImage]` and
  `startIndex: Int`. It holds:
  - a `TabView` with `.tabViewStyle(.page)` for swiping;
  - a zoomable image per page;
  - a toolbar with close and `ShareLink`.
  Present it from `ChatView` with `.fullScreenCover(item:)`, like the camera's
  `.fullScreenCover` in the same file. `MessageRow` is a private struct
  further down that file. Pass a closure up, `onOpenImages: ([UIImage], Int)
  -> Void`, the same way it receives `loadImage`. Don't present from inside
  the row.
- **Run `xcodegen generate`** after adding the file. Otherwise it isn't
  compiled and the suite stays green without it. Check that the Swift test
  count went up.

### Web

- `web/src/app/MessageRow.tsx`:
  - `BubbleImage` renders one image or placeholder.
  - `UserBubble` builds `keys` (attachment ids → `att:<id>`, or the message's
    own keys) and `have`.
  - Make each loaded `<img>` a button-like tap target: `role="button"`,
    `tabIndex=0`, Enter/Space open it. On tap, open the viewer with the
    message's loaded keys and the tapped index.
- Bytes come from `useImageUrl(store, key)` in `web/src/app/hooks.ts`, which
  returns a blob URL.
- **New file** `web/src/app/ImageViewer.tsx`: a fixed full-viewport overlay
  (`position: fixed; inset: 0; background: #000`). It holds:
  - the image with `object-fit: contain`;
  - prev/next by horizontal swipe (pointer events) and by arrow keys;
  - close, download, and an "n of m" label.
  Render it with a portal to `document.body`, so the chat's scroll container
  doesn't clip it.
- Styles go in `web/src/app/styles.css`. Use the colour tokens already there,
  and keep glyph-in-circle controls in px (see the comment above
  `.retry-arrow`).
- Look at `Composer.tsx` for how the codebase handles object URLs. The viewer
  must **not** revoke the URLs `useImageUrl` owns.

## Tests

- **Swift Testing:** test whatever logic you factor out, e.g. "which slots are
  openable, and what is the start index when some tiles are still
  placeholders". The open order must skip unloaded slots but keep message
  order. SwiftUI gestures themselves aren't unit-tested here, so check those
  in the simulator.
- **Playwright** (`web/e2e/`, next to `composer.spec.ts`). Tests to add:
  - send 3 images, tap the second tile, and the viewer shows "2 of 3";
  - ArrowRight shows 3 of 3;
  - Escape closes it;
  - Back closes it;
  - the download link has the `download` attribute and a blob href;
  - a placeholder tile does not open the viewer.
  Run on both projects (desktop and pixel7).
- **vitest** for any pure helper, such as the index mapping.
- **Verify the verification:** break the feature (e.g. comment out the tap
  handler) and watch the new e2e fail before trusting it.

## Docs and housekeeping (same PR)

- `docs/chat-ux-contract.md`: add **CMP-10** (the next free number; CMP-9 is
  the last today) describing behaviours 1–6 above, tagged `[test]`.
- `changes.json`: one `feature` entry, `part: "app"`. Read `_readme` for the
  next id; ids only go up.
- `docs/tasks/ios-image-input.md`: a one-line pointer to this doc.
- No `identity/CLAUDE.md` change: this is display only, and Marlow doesn't
  touch it.

## Not in scope

- Viewing images in **assistant** bubbles. They don't carry images today.
- Originals or higher resolution, editing, deleting, or forwarding to another
  chat.
- Saving a draft's staged photos (see `ios-chat-ux-gaps.md` §20).

## Done means

- Both clients build, and all local suites pass. Quote the counts: pytest,
  Swift Testing, vitest, Playwright composer and viewer specs on desktop and
  pixel7.
- Checked visually in the iOS simulator (`-uiFixture idle`, send a few photos
  from the photo library, then tap) and in the web client at pixel7 size.
  Check specifically that long-press copy still works on iOS.
- `/code-review high` run on the diff, and its findings fixed or listed.
- Not checked until Mike tries it: how it feels on a real phone, swipe-down
  on iOS, and Android long-press save/copy in Chrome. Say so in the PR text.
  Don't open the PR without asking Mike.
- Repo rules: name no person but Mike, and name no other AI assistants, in
  files or commits.
