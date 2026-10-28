# Task — image input from iOS (paste, photo roll, camera)

**Status:** ✅ shipped. Step 1 (paste + photo roll) merged 2026-08-11; step 2
(camera) shipped on both clients 2026-10-23; **multi-image messages (up to 6)
shipped on both clients 2026-10-24**, device-checked against Mike's box on the
iPhone and in the Android emulator's installed PWA, announced in What's new
(`changes.json`). Next: tap a sent photo to view it full screen, specced in
`image-viewer.md`.

Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

Captured 2026-08-08 from Mike: attach an image three ways (paste, photo roll,
camera). It touches the wire protocol, `boundaries.py` and the client, which is
why it has its own doc rather than a line in `ios-chat-ux-gaps.md`.

## Settled decisions

| Question | Answer | Why |
| --- | --- | --- |
| CLI contract | **A path in the prompt text. `invoke.py`'s argv is unchanged.** | Spiked, below. |
| Downscale | **On the device, before upload** (Mike, 2026-08-11) | 12MP HEIC over a van-dead-zone tailnet buys the model nothing. |
| Long edge / quality | **1568px, JPEG q0.6** | 1568 is the long edge above which the model's own tiling stops gaining detail. q0.8 until 2026-08-27; see the size note. |
| Wire shape | **base64 in the existing `POST /v1/message` JSON** | See the size note. |
| Images per message | **Up to 6** (Mike, 2026-10-23; was one) | A second user asked after camera shipped. Wire `images: [{b64, mime}]` (the single `image_b64`/`image_mime` pair is still accepted); 12MB decoded total beside 6MB per image; any bad image rejects the whole message. Files are `<turn_id>/0.jpg`, `1.jpg`, …; one `[image attached: …]` line each. |
| Format on the wire | **JPEG only**; the phone transcodes HEIC/PNG | One decoder path server-side. |
| Storage root | **`~/.marlow/attachments/`**, *not* under `MARLOW_DATA_DIR` | Trap 1. Load-bearing. |

A bad image is a *hard* fail (413 over 6MB decoded, 400 for non-JPEG), unlike
the `timezone` warn-and-drop: silently dropping the picture and sending the
caption alone gets a reply about an image Marlow never saw, worse than an error.
Optional wire fields keep an older client working.

## The CLI spike — answered 2026-08-11, box, CLI 2.1.223

The gating unknown was whether image input needed a new CLI contract. It does
not. Given a bare filesystem path in the prompt with **no instruction to use any
tool**, the CLI called `Read` on it unprompted, and `Read` hands real pixels to
the model: a fixture carrying an invented string and two coloured shapes came
back with the string and both colours correct. So the prompt needs one extra line
and nothing else, and every hard-won decision in `invoke.py`'s args list (argv
injection, `--` separation, the `--resume=` binding, the `--strict-mcp-config`
removal) stays untouched.

**What the spike surfaced is that permissions, not capability, are the
constraint.** Round one put the fixture in `/tmp` and every `Read` was denied:

- **`--allowedTools Read` on the command line did NOT override the `settings.json`
  deny.** No per-invocation escape hatch; wherever attachments live needs a real
  allow rule.
- The denial arrived as `exit=0` / `is_error=false` with a non-empty
  `permission_denials`, the CLAUDE.md invariant demonstrating itself. Any test
  asserting "the image was read" must assert on `permission_denials`, **not** exit
  status, or it passes against a build that reads nothing.

## Traps

**1. Attachments must not live under `MARLOW_DATA_DIR`.** `boundaries.SECRET_PATHS`
contains `str(settings.data_dir)`, so `Read` there is hard-denied and **wins over
any allow**; and that tuple drives `invoke.py`'s bwrap `--tmpfs` list, so the file
wouldn't exist in the CLI's namespace at all. The failure mode is a "file not
found" that survives every permission fix. `~/.marlow/attachments/` is a sibling
of `~/.marlow/apns/`; the apns *directory* is a secret path, so don't nest under
it either.

**2. The allow rule has a twin.** `tests/test_marlow_settings.py` holds
`settings.json` and `boundaries.py` in lockstep; adding the read grant means
editing both plus the installed policy on the box. The `ATTACHMENT_ROOT` is a
separate tuple from `SECRET_PATHS`/`AGENT_CONFIG_PATHS` (it drives an allow, not a
deny). **`file_policy.py`'s `vet_file_tool` needs a `Read`-only,
`ATTACHMENT_ROOT`-scoped branch too:** a `settings.json` allow is inert on its own
unless the hook's vetter independently agrees (the established pattern, WebSearch
/WebFetch 2026-08-06); without it every image send raises a phone permission
prompt for a Read that was always going to be approved.

**3. `draft` and the send path.** `ChatView.submitDraft()` has non-obvious
ordering for keyboard dictation (`ios-minimal-client.md`). An attached image must
be captured in the same run-loop hop as the text and cleared on the same path, or
a send can carry one without the other.

## What was built (the seams that mattered)

**1. Storage + boundaries.** `settings.attachments_dir`, 0700 at startup;
`ATTACHMENT_ROOT`; the `settings.json` mirror; the vetter branch (trap 2).

**2. Wire + queue.** The file is written to `<attachments>/<turn_id>/0.jpg`
**before** `enqueue`, so it exists when the worker reaches it. `Turn` carries the
paths (a `tuple[str, ...]` was first built "for future multi-image", reverted in
`/code-review medium` as speculative machinery since every consumer read index
`[0]`; multi-image then made it real, as `attachment_paths`). **Cleanup** in the
worker's drain loop, **unconditional on status** (an ERRORed turn leaves the same
bytes), plus a startup sweep of anything older than 24h, because a restart drops
in-flight turns and would orphan their files forever.

**3. Prompt.** One `[image attached: /home/marlow/.marlow/attachments/<turn_id>/0.jpg]`
line prepended to the turn-context block, not a change to the args list.

**4. Client.** Downscale with ImageIO `CGImageSourceCreateThumbnailAtIndex`
(avoids fully decoding a 12MP original just to shrink it). Photo roll via SwiftUI
`PhotosPicker` (no Info.plist string); paste via `UIPasteboard.general.image`.
**Transcript cache:** the image can't go inline in `ChatStore.persist()`'s JSON;
the downscaled JPEG is copied into Application Support keyed by `client_msg_id`
and the filename persisted. `ChatStore.retry(_:)` reads text off the paired user
message by `clientMsgId`, and the image needs the same handle, which is why it is
keyed that way.

**5. Camera.** `UIImagePickerController` with `.camera`, `NSCameraUsageDescription`.
Deliberately last: it adds a permission prompt and a capture UI to a path already
proven.

**Acceptance #4: the attachment file is gone after the turn reaches DONE, and
after ERROR.** The others: a photo-roll pick and a pasted screenshot each get a
reply about the image content with `permission_denials` empty; the orphan sweep
removes a planted stale directory; an airplane-mode send auto-retries the *same*
image once under one `client_msg_id`; an oversized or non-JPEG upload surfaces
413/400 rather than sending a caption about an invisible image.

## Size note: inline base64 vs. a separate upload

Inline is one round trip, one idempotency key, and no orphaned-upload problem. The
cost is that auto-retry re-transmits the bytes on every attempt over exactly the
flaky link that triggered it (the server dedupes on `client_msg_id`, so wasted
transfer, not a duplicate turn). ~300KB post-downscale was assumed acceptable.

**Measured 2026-08-27 (Mike): 431KB at long edge 1568**, above that assumption.
His call: **the pixel dimensions are right, the byte size is not; target
~200KB.** 1568 stays, being set by the model's tiling, not transfer cost.

**Shipped as q0.8 → q0.6 the same day, and it does not reach 200KB.** The ImageIO
quality curve was measured first, on smooth gradients and a detail-saturated
synthetic: q0.6 returns roughly **60–75% of q0.8's bytes**, so 431KB lands near
**~300KB**. 200KB by quality alone needs about q0.4, which visibly damages text,
and these are frequently screenshots where text is the payload. 0.6 is where each
step costs more quality than it saves bytes. **If ~200KB is a hard requirement the
lever is pixels, not compression,** which reopens the 1568 decision; do that
deliberately, asking the model-quality question out loud. This constant was not
handed to an implementor alongside `ios-poor-network-timeouts.md`, because how
much JPEG quality is acceptable is judged by looking at the output. If a bad link
is still the problem once bytes are halved, a separate upload endpoint is the fix;
don't build it speculatively.

## Security note

An image is **the first content on this box that reaches Marlow as data without
having passed through Mike's keyboard.** The `settings.json` connector comment
already reasons about Gmail as a prompt-injection carrier; a screenshot of a web
page is another, and text rendered in an image isn't obviously text to anything
upstream of the model. Not a blocker (the source is Mike's own paired phone), but
it is why this wanted `/security-review` alongside `/code-review high`.

## Findings and observations

**Step 1.** Two rounds of `/code-review medium` and one `/security-review` ran
clean (findings fixed, zero security findings). It was unit-tested against a fake
`claude` binary and fake `ChatAPI`; at merge, the real CLI's `Read` on a live attachment
had only been exercised by the spike (device runs below came later).

**Step 2 (camera).** A "Take Photo" item in the composer's + menu (hidden where
there is no camera, so never in the simulator), feeding the paste path's `UIImage`
downscaler; no server change. Building it found a latent downscaler bug: it sized
from `cgImage` pixels, which ignore orientation, so a portrait camera shot (sensor
pixels tagged `.right`) came out squashed to landscape. Paste never hit it because
pasted images are `.up`. Fixed, with a test that failed first. **Web/Android first
shipped without code, wrongly:** the assumption was that `accept="image/*"` gets
Camera in Android Chrome's chooser; on a current phone it opens the system photo
picker, which has none (the Android user, 2026-10-23). So on a touch device "+"
opens Take photo / Choose photo, the first through a separate
`capture="environment"` input; desktop keeps the one-click dialog (contract
CMP-9). **Parked:** a user who denied camera access still sees Take Photo and gets
the system's locked placeholder with no pointer to Settings; build a notice if
anyone hits it. Observed: a camera photo from Mike's iPhone, correctly described;
on the Android emulator Take photo opened the camera and the photo went through.
**Never observed:** the menu surviving the soft keyboard closing (the emulator
kept hiding its keyboard, since the Mac's counts as hardware; only Playwright
CMP-9 covers it), nor a real Android phone's camera.

**Multi-image (2026-10-24).** `/code-review high` found 8 issues; 4 fixed on iOS
(remove target covered the thumbnail, a failed pick dropped silently, a single
undownloaded image drew nothing, a remove keyed by a stale index).
`/security-review`: no findings. Observed in the simulator: the picker says
"Select up to 6" then single-select at 5; tiles append in pick order; tapping a
tile leaves it while its X removes only it; a 7th shows the notice and keeps all
6; sending draws a 3x2 square-cropped grid. **On Mike's iPhone against the box:**
multi-pick, camera and paste append; sends of 6, 2 and 3 photos with a caption and
3 without; every turn Read every image (6/6, 2/2, 3/3, 3/3) with no error results;
an airplane-mode send auto-retried into one transcript row, one reply, no
duplicate `client_msg_id`; the same with a force-quit after send and a relaunch
online (3 photos resent, one row, Read 3/3). The staged row was too small to
remove from with confidence at 44pt in one line, so it is now three across at 64pt
("much better"). **In the Android emulator's PWA:** a 7-photo pick keeps 6 and says
"Only the first 6 added"; staged three across at 64px; a 5-photo send with a
caption drew a 3+2 grid and the reply named all five in order (Read 5/5); an
airplane-mode send of 4 drew a 2x2 grid, showed Sending…, and landed once (one
row, Read 4/4); camera then library append in order; the iPhone's 3- and 6-photo
rows synced to Android as grids. **Not yet observed:** paste on Android
(Playwright covers it); a force-quit relaunch on Android; the iPhone showing
Android-sent rows; a sent 2-photo grid seen on Android.

**Known, not fixed (Mike, 2026-10-24):** a client that synced a multi-image row
while still on a pre-multi-image build cached only the first image (legacy
`attachmentId`) and keeps showing one photo, because sync never re-fetches a row
it holds (seen on Mike's Mac Chrome app; the server has every image). The fix, if
a felt symptom returns: on load, drop cached server user rows that have
`attachmentId` but no `attachmentIds`, and lower `latestSeq` to just below the
lowest of them, so catch-up merges them again; same on both clients.

**Left open from the review:** an image-only message whose files are gone at retry
sends empty text and fails with a 400 instead of saying so (both clients); web
fetches a row's images in parallel while iOS fetches in order; a phone still on
`main` shows only the first image of a multi-image row.

**Not scoped:** whether Marlow's *replies* can contain images.
