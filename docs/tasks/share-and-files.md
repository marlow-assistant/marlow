# Task — "Share to Marlow", and files beyond photos

**Status:** not started. Specced 2026-10-24 for implementor sessions. It
builds on multi-image messages (merged 2026-10-24), so it is ready to start. It is
four phases, and each one is its own PR.

## Why (Mike, 2026-10-24)

On a phone, the way you move something between apps is the share sheet.
"Save the file, then find it again in Files" is the fallback, not how people
work. Today Marlow takes only photos, and only from its own composer.
Wanted:

- **Marlow in the share sheet**, on iOS and on Android: share a PDF from
  Mail, a page from Safari, a spreadsheet from Files, or photos from Photos,
  straight to Marlow with an optional note.
- **Wider file types:** text, CSV and PDF, plus Office documents (Word,
  Excel, PowerPoint). Mike's view: accepting a wide range is worth adding a
  few conversion tools to the box, the same way Parakeet speech-to-text
  already lives there. Marlow becomes more capable if a .docx is something
  it can actually read.

**The rule that shapes everything below (CMP-4): Marlow must never get a
file it can't read.** A reply to a caption about a file Marlow never opened
is worse than an error. So types come from an **allowlist**, every accepted
type has a reading path (native Read, or a server-side conversion to text),
and a conversion that fails rejects the whole message, so nothing is sent
with a piece missing.

## Accepted types

| Kind | Types | How Marlow reads it |
|---|---|---|
| Photos | JPEG, PNG, HEIC, WebP | The client converts to JPEG with the existing downscaler, then the existing photo path (`images`) |
| PDF | `application/pdf` | The CLI's Read opens PDFs natively. A long PDF must be read in page ranges (Read takes a `pages` argument, at most 20 pages a call). Tell Marlow so in the identity. |
| Plain text | `.txt .md .csv .tsv .json .log` | Read as is |
| Word | `.docx` | The server converts to Markdown at upload |
| Excel | `.xlsx` | The server converts each sheet to a Markdown table (row cap, see below) |
| PowerPoint | `.pptx` | The server converts slide by slide: title, body text, speaker notes |

**Out, for now:**
- Legacy binary `.doc/.xls/.ppt`: these need LibreOffice, roughly 500MB on
  the box. Add them only if someone actually misses them.
- Pages, Numbers and Keynote.
- Zip archives, audio and video.
- Anything the allowlist doesn't name.

Out-of-list types get a clear refusal ("Marlow can't read .pages files yet"),
never a silent drop.

## Limits

- **At most 6 items per message, photos and files combined.** That is
  the photo path's `_MAX_IMAGES`, made one shared cap.
- **Per file: 20MB decoded** for files. Photos keep their 6MB.
- **Total: 25MB decoded** per message, up from 12MB, to fit one big PDF.
- **Converted text is capped at about 200,000 characters per file**, cut at
  a paragraph or row boundary. The `.md` says at its top that it was cut, and
  where.
- **Excel:** at most 2,000 rows per sheet and 20 sheets, with the cut noted
  the same way.

Every one of these is a module constant next to `_MAX_IMAGES`. The client
mirrors each one, with a comment pointing at the server as the authority.

---

## Phase 1 — server: files on the wire, conversion, transcript

### Wire

In `server/app.py` `MessageRequest`:

- Add `files: list[dict] | None`, each entry `{b64, mime, name}`. Keep
  `images` exactly as the photo path has it.
- Validate the way `_decode_images` does: decode and check everything before
  writing anything, `files[i]` named in every error, and a 400 for an empty
  list. Check `mime` against the allowlist, and require it to agree with the
  file's own signature:
  - PDF starts with `%PDF-`;
  - docx/xlsx/pptx are zips containing `[Content_Types].xml` and the right
    main part;
  - text decodes as UTF-8, or as UTF-8 after a BOM strip.
- **`name` is display only.** Strip control characters, newlines and `[]`,
  cap it at 120 characters, and fall back to "file". It never becomes part
  of a path.

### Storage and prompt

`server/attachments.py`:
- Write files beside the images in `<turn_id>/`, as `<n>.<ext>` with `ext`
  taken from the **allowlist entry**, never from the client's name.
- After conversion, write the text beside the original as `<n>.md`.

`Turn` (`server/queue.py`) and `run_claude` (`server/invoke.py`):
- `attachment_paths` stays the photo path list.
- Add a parallel `file_attachments: tuple[FileAttachment, ...]`, where
  `FileAttachment(path, name, kind, text_path | None)`.
- Coalescing (`_can_coalesce`) must also exclude turns with files, and must
  list the new field in `_COALESCING_CLASSIFIED_TURN_FIELDS`. A test there
  fails if a Turn field isn't classified, so it will tell you.

The prompt gets one line per file, in the same block as the image lines:

```
[file attached: /…/<turn>/2.pdf — "Booking confirmation.pdf", PDF]
[file attached: /…/<turn>/3.docx — "Trip notes.docx", Word document; text version: /…/<turn>/3.md]
```

`boundaries.ATTACHMENT_ROOT` already allows Read under the attachments
directory, so the agent can read these paths with no boundary change. Check
that with a test that runs the path vetter on a `.md` path.

### Conversion: `server/convert.py`, run confined

These parsers read hostile input: a stranger's .docx, forwarded to you. A
parser bug must not run inside the server process, which holds the device
tokens and APNs key.

- Run each conversion as a **subprocess** (`python -m server.convert
  <kind> <in> <out>`) under **bubblewrap**:
  - no network (`--unshare-net`);
  - only the input file (read-only) and the turn's output directory
    (writable) bound in;
  - `RLIMIT_AS` of about 512MB;
  - a 30-second wall-clock timeout.
  Copy the argv style of `_confinement_prefix` in `server/invoke.py`.
  Bubblewrap is already installed on every box.
- Libraries, added to `server/requirements.txt` (installed by `update.sh`'s
  pip step): `python-docx`, `openpyxl` (with `read_only=True,
  data_only=True`), `python-pptx`. All are pure Python on top of `lxml`
  wheels. Import them only inside `server/convert.py`, the way
  `server/transcribe.py` imports `sherpa_onnx`, so server startup doesn't
  pay for them.
- **Zip-bomb guard before parsing:** sum `ZipInfo.file_size` across the
  archive and refuse anything over 200MB uncompressed, or over 5,000
  entries.
- **XXE:** make sure no external entities or DTD fetches happen. Add a test
  with a .docx carrying an external-entity payload. The conversion must not
  read the referenced file, and with `--unshare-net` it can't fetch one
  anyway.
- Output: plain Markdown.
  - Headings keep their level.
  - Tables become Markdown tables.
  - For Excel, each sheet is a `## Sheet name` with a table, using cell
    values, not formulas.
  - For PowerPoint, each slide is `## Slide n: title`, then body text, then
    `Notes:`.
- **Failure:** the timeout, a non-zero exit, empty output, or a refused
  archive all reject the message with 422, `files[i] could not be read`.
  Nothing is enqueued. This follows the same all-or-nothing rule as images
  (CMP-4).

### Transcript

`server/transcript.py`:
- Add `name TEXT` and `size INTEGER` columns to `transcript_attachments` with
  the `ALTER TABLE … ADD COLUMN` migration pattern the file already uses for
  `quote` (around line 174). Both stay nullable: an old row is an image with
  no name.
- `append_user` takes `(path, mime, name, size)`.
- Rows return `attachments: [{id, mime, name, size}]`.
- Store the **original** file, not the `.md`. The download endpoint already
  serves non-image types with `Content-Disposition: attachment` and
  `nosniff`, so it needs no change.

### Identity (same PR, per the repo `CLAUDE.md`)

`identity/CLAUDE.md` "Images" becomes "Attachments":
- Open every attached photo or file before answering, in order.
- For a Word, Excel or PowerPoint file, read its text version: that is the
  whole content.
- Read a long PDF in page ranges.
- If something won't open, say so. Never answer about content you haven't
  read.

### Tests (pytest)

- **Each type round-trips:** decode, convert, prompt lines, transcript row
  with name and size.
- **Refusals:**
  - a mime/signature mismatch (a .exe named .pdf);
  - an out-of-list type;
  - an oversize file;
  - more than 6 items combined;
  - a zip bomb;
  - an XXE payload;
  - a conversion timeout, using a fake converter that sleeps;
  - a converter crash.
  Each one leaves **nothing written and nothing enqueued**.
- A client-supplied `name` with `../`, newlines or `]` appears only
  sanitized in the prompt, and never in a path.
- Files never coalesce.
- Old rows without `name`/`size` still serialize.

---

## Phase 2 — composer and bubble, both clients

The in-app path, and the chips the share flows will reuse.

- **Composer:**
  - iOS: add a "Choose File" item in the + menu, using `.fileImporter`
    with UTTypes from the allowlist and multiple selection.
  - Web: widen the file input's `accept` attribute to the allowlist.
  - Photos still go through the downscaler. Other files are staged as
    **chips** in the same three-across grid as the photo thumbnails: a 64pt
    tile with a type icon, the name (truncated in the middle) and an X.
    The 6-item cap counts photos and files together.
- **The client checks type and size before staging** and refuses with a
  notice ("Marlow can't read .pages files yet", "That file is over 20MB").
  The server check stays the authority.
- **Send:** `files: [{b64, mime, name}]` alongside `images`. The
  all-or-nothing save for retry (CMP-6) covers files as well as images.
  Saved as:
  - iOS: `<clientMsgId>-f<i>.<ext>`;
  - web: IndexedDB keys `<id>:f<i>`.
- **Bubble:** after the photo grid, one chip row per file with icon, name
  and size. Tap opens it:
  - iOS: QuickLook (`.quickLookPreview`);
  - web: the download endpoint, opened via a blob URL from the
    authenticated fetch. The token never goes in a URL.
  Files from other devices download on tap, not eagerly. Files can be
  large, and unlike photos they aren't needed to draw the bubble.
- **Contract:** add CMP rules for file staging and chips to
  `docs/chat-ux-contract.md` (next free numbers), tagged `[test]`.
- **Tests:** Swift Testing (staging cap with mixed items, the retry
  save/load of files, legacy rows); vitest (the same); Playwright (stage a
  PDF and a docx chip, send, request body shape, bubble chip).

---

## Phase 3 — iOS share extension

- **New target `MarlowShare`** in `ios/Marlow/project.yml`: `type:
  app-extension`, `NSExtensionPointIdentifier:
  com.apple.share-services`, bundle id `com.mikemason.Marlow.Share`.
  Copy the `MarlowNotificationService` target's structure, including
  `DEVELOPMENT_TEAM`, `CODE_SIGN_STYLE: Automatic` and `SKIP_INSTALL`, and
  the comment on why the bundle id must be a child of the app's. Run
  `xcodegen generate`.
- **Entitlements:** copy `MarlowNotificationService.entitlements`: the App
  Group `group.com.mikemason.Marlow` and the keychain group
  `$(AppIdentifierPrefix)com.mikemason.Marlow.shared` **only**. Don't touch
  the app's own entitlements file. Its keychain group order is load-bearing,
  and its comment explains how reordering un-pairs the phone.
- **Credentials:** the extension compiles in `ios/Marlow/Shared/`.
  - It reads the token with `SharedCredential` (the app mirrors it on every
    launch).
  - It reads the server with `PairedHost.current()`.
  - With no token, it shows "Open Marlow and pair first" and a Close button.
- **Activation rule (`NSExtensionActivationRule`):** up to 6 attachments,
  restricted to the allowlist's UTTypes, plus web URLs and plain text.
  Marlow then doesn't appear in the share sheet for things it can't take.
- **UI:**
  - SwiftUI hosted in the extension's view controller.
  - The staged items as Phase 2 tiles and chips.
  - An optional caption field.
  - Cancel and Send.
  - Shared text or a URL pre-fills the caption, so sharing a Safari page
    sends its URL for Marlow to fetch. That is not a file.
  - Load items one at a time and downscale photos as they come in: share
    extensions run with a tight memory limit, around 120MB.
- **Send:** POST `/v1/message` directly with a fresh `client_msg_id` and the
  same body shape as the app. Use `APIClient` if it compiles into the
  extension. If not, move the request-building part into `Shared/`.
  - **On success**, close. The main app shows the message through the
    normal transcript sync: it arrives as a row from "another device", so
    `TranscriptMerge` inserts it, downloads its photos, and polls for the
    reply. **Check this path specifically.** It's the one piece of glue this
    phase relies on without adding code.
  - **On failure** (offline, timeout, 5xx), write the message to a **share
    outbox** in the App Group container: one directory per message holding
    the JSON body plus the files, the same one-file-per-entry pattern and
    reasoning as `Shared/TurnInbox.swift`. Close with "Saved — it'll send
    when you open Marlow."
  - **On a 4xx**, show the server's detail and keep the sheet open. Don't
    queue it: a retry can't fix it.
- **The app drains the share outbox** on launch and on becoming active.
  Each entry is sent with its saved `client_msg_id`, which the server's
  dedup makes safe to repeat. Delete the entry after a 2xx or a 4xx (a 4xx
  gets a notice). Keep it after a transient failure.
- **Tests (Swift Testing):**
  - the outbox write/drain/delete cycle;
  - drain idempotency with the same `client_msg_id`;
  - activation-rule UTTypes match the allowlist;
  - the no-token path.
  The share sheet itself needs a device. The simulator can show the
  extension from Photos and Files, so check it there.

---

## Phase 4 — Android: Web Share Target

The web client is a PWA. Chrome on Android lists an **installed** PWA in the
system share sheet when its manifest declares `share_target`.

- **`web/public/manifest.webmanifest`:**

  ```json
  "share_target": {
    "action": "/share-target",
    "method": "POST",
    "enctype": "multipart/form-data",
    "params": { "title": "title", "text": "text", "url": "url",
                "files": [{ "name": "files", "accept": [ …allowlist mimes and extensions… ] }] }
  }
  ```

- **`web/src/sw.ts`:** handle `fetch` for `POST /share-target`:
  - Read the `FormData`.
  - Put the files and text in IndexedDB under a fresh share id. Reuse the
    persistence layer's database if the service worker can open it; if not,
    use a small dedicated store.
  - Respond `303` to `/?share=<id>`.
- **The app, on load with `?share=`:**
  - Stage the items in the composer through the Phase 2 path (same checks,
    same notices).
  - Pre-fill the caption from `title`/`text`/`url`.
  - Remove the share entry and the query parameter.
  - **Don't auto-send:** the user sees what was shared, adds a note, and
    taps Send. Unlike iOS, the PWA *is* the app, so there is no separate
    sheet to send from.
- **Only installed PWAs get this.** In Settings, or the install prompt if
  there is one, say "Install Marlow to your home screen to share to it."
- **Tests:**
  - a vitest for the service worker's handler: form data in, IndexedDB
    entry, 303 out;
  - a Playwright test that loads `/?share=<id>` with a seeded entry and sees
    the staged chips and caption.
  The real share sheet needs the Pixel emulator with the PWA installed.

---

## Housekeeping (per phase)

- `changes.json`:
  - Phase 2: "Attach PDFs, documents, spreadsheets and text files."
  - Phases 3 and 4: "Share to Marlow from any app."
- `docs/chat-ux-contract.md`: the CMP rules named above.
- `docs/tasks/message-retention-setting.md`: files are covered by the same
  transcript prune, and their converted text by the session-file prune. Add
  a line there saying so.
- `/code-review high` on every phase. **`/security-review` on Phases 1 and
  3**: hostile-file parsing, the confinement, and the extension's access to
  the token.

## Not in scope

- Legacy Office formats, iWork, archives, audio and video.
- OCR of scanned PDFs. The CLI's Read sees PDF pages as images, so Marlow
  can read a scan visually anyway.
- Sending files *from* Marlow to the user. Separate.
- A share extension that waits for Marlow's reply. It sends and closes, and
  the reply arrives in the app with a push as usual.

## Done means (each phase)

- All local suites pass, with counts quoted.
- Checked in the simulator (iOS) and at pixel7 (web), with a list of what
  only a device can show:
  - the real share sheets;
  - memory limits on big files in the extension;
  - Android's installed-PWA share target.
- Don't open a PR without asking Mike.
- Repo rules: name no person but Mike, and name no other AI assistants.
