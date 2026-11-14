# Share and files — the wire contract (build sheet)

Companion to `share-and-files.md`, pinned 2026-11-14 so the server and both
clients could be built at the same time. It stays the reference for the
wire: where the two disagree, this file wins.

## Request: `POST /v1/message`

Alongside the existing `images: [{b64, mime}]` (unchanged, JPEG only):

```json
"files": [{"b64": "<base64>", "mime": "<client's guess, may be empty>", "name": "Trip notes.docx"}]
```

- Optional. Absent or omitted means no files. An empty list is a 400.
- **At most 6 items, `images` and `files` counted together.**
- **Per file: 20MB decoded.** Photos keep their 6MB.
- **Total: 25MB decoded**, images and files together.
- `mime` decides nothing on the server; send what the platform gives, or "".
- `name` is display only. The server strips control characters, newlines,
  `[`, `]` and `"`, caps it at 120 characters, and uses "file" if nothing
  is left.

Errors, each naming `files[i]` (or `images[i]`):

| Status | When |
|---|---|
| 400 | empty `files` list, an entry without string `b64`/`mime`/`name`, bad base64, an empty file, more than 6 items |
| 413 | a file over 20MB, or the message over 25MB |
| 422 | `files[i] could not be read`: an Office file whose conversion failed (timeout, crash, empty output, zip bomb) |

**No type is ever refused.**

## Kinds (server-sniffed) and the stored `mime`

| Kind | Stored `mime` | Prompt label |
|---|---|---|
| Image | `image/jpeg`, `image/png`, `image/gif`, `image/webp` | `image` |
| PDF | `application/pdf` | `PDF` |
| Word | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | `Word document` |
| Excel | `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` | `Excel spreadsheet` |
| PowerPoint | `application/vnd.openxmlformats-officedocument.presentationml.presentation` | `PowerPoint presentation` |
| Text | `text/plain` | `text` |
| Other | `application/octet-stream` | `<size>, a type you can't read` |

## Transcript rows

User rows' `attachments` entries become `{id, mime, name, size}`:

- `mime` is the stored value above, never the client's.
- `name` and `size` (bytes, of the original) are `null` on old rows, and on
  photos sent through `images` (`name` null; `size` is filled for new ones).
- Photos come first in `idx` order, then files, in the order sent. A file
  entry always has a `name`; that, not its position, tells a client which
  entries are its own send's files (an attachment whose copy failed is
  skipped, which shifts the positions).

`GET /v1/transcript/attachment/{id}` is unchanged: it serves the stored
`mime`, with `Content-Disposition: attachment` for anything not `image/*`,
and `X-Content-Type-Options: nosniff`.

## Client rules

- **No client filters by type.** Clients check the item count (6, photos and
  files together) and size (20MB a file, 25MB a message) before staging, with
  a notice. Each constant carries a comment naming `server/app.py` as the
  authority.
- Anything the client can decode as an image goes through the existing
  downscaler and `images`, whichever button picked it. Everything else goes
  in `files`. "An image" means an image format: iOS's ImageIO also draws a
  PDF's first page, so the decoded type must conform to `public.image` too,
  or a PDF goes out as a JPEG of page one.
- **Opening a file chip:**
  - iOS: QuickLook on a local copy, named with an extension that matches
    the stored `mime` (`.pdf`, `.txt`, `.docx`, …) or the name's own
    extension for `application/octet-stream`.
  - Web: fetch with the bearer header (never a token in a URL). Open inline
    in a new tab from a blob URL **only** for `text/plain`,
    `image/jpeg`, `image/png`, `image/gif`, `image/webp`, with
    the Blob's type taken from a fixed client-side map keyed by the row's
    `mime` (text is `text/plain;charset=utf-8`), never from the response's
    `Content-Type`. Everything else, PDF included (the
    page CSP's `object-src 'none'` reaches a blob tab), downloads via `<a download="<name>">`
    from a Blob typed `application/octet-stream`. Why: a blob URL runs in
    the web client's origin, beside the device token.
