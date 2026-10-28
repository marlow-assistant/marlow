# Server-owned transcript

**Status: shipped.** Designed 2026-10-01. Step 1 (server, deployed) and step 2 (iOS, device-checked on Mike's phone; see `ios-server-transcript.md`) are done, and step 3 (removing `/v1/pending`) is merged. This was the prerequisite for `portable-web-client.md`.

Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

Mike's calls, same day: iOS switches to the server transcript; phone-local history may be lost (this build starts clean, no migration); retention is **30 days**.

## The problem

The conversation was only durable on the phone: `ChatStore` wrote it to a local file, and the server kept only `_turns`, capped at 256 and in memory. Worse, `/v1/pending` handed each unsolicited turn out **once, globally** (`ready_undelivered_turns()` flipped one `delivered` flag). With two clients, a briefing appears on whichever polls first, and anything sent from one device never appears on the other.

## Design

### One append-only store, and the client holds the cursor

`server/transcript.py` keeps a SQLite file in `data_dir` (stdlib `sqlite3`, WAL). `data_dir` is already in `SECRET_PATHS`, so the agent can't read it: the transcript is a client surface, and the agent has its sessions. Each row is one bubble, **immutable once written**, with an autoincrement `seq` as the sync cursor. Attachments are a separate table because a message will carry several files, not only pictures.

`answers` on an assistant row is a JSON array of the **user-row `turn_id`s the reply resolves**, and it is **not** the row's own `turn_id`: a coalesced reply answers every message merged into it, and a retry's reply answers the *original* user row rather than its own fresh turn. That is what lets a client tell a resolved user row from one in flight.

**There are no per-device delivery flags.** Each client remembers the highest `seq` it has seen and asks for anything after it. That removes the global `delivered` race, a new device costs the server nothing, and since the server is the only writer, `seq` order is display order. That retired the insert-by-time workaround in `ingestUnsolicited` (gaps item 7): arrival order *is* time order.

### When rows are written

- **User row:** in `POST /v1/message`, after validation and before `enqueue()`, carrying the turn_id the client will be given. It **reuses the queue's live turn for this `client_msg_id` when there is one** (`WorkQueue.reusable_turn_id`) so a row is never stamped with an id `enqueue` then discards. A retried `client_msg_id` hits a unique index and writes nothing. That index is **permanent** for the retention window, unlike the queue's 600s dedup window: the transcript records that a message was *sent* once, not that it *ran* once. Coalesced (MERGED) messages each keep their own user row.
- **A retry that makes a new turn** (the first ERRORed, or the queue forgot it) finds the existing row under a different turn_id. The row is not rewritten; the mapping lives on the new turn (`Turn.answers_turn_ids`), so its reply names the original row.
- **Assistant row:** in `_drain`, at the terminal transition, **before** the `TURN_READY` push, so a push-woken client finds the row already there. DONE writes `result_text`, ERROR writes the error (`""` when the server has no text for it). `answers` is the surviving turn's own user-row id, every turn MERGED into it, and any original row a retry stands in for; cron replies answer `[]`. MERGED, CANCELLED and compaction write nothing (the same exclusion as every other delivery surface). `post_system_message` writes an assistant row with `source="system"`.
- **A restart orphans user rows.** `_turns` is in memory, so at startup any user row older than the process that no assistant row answers names a turn that no longer exists. `WorkQueue.start()` writes a `status=error`, `source=system` row answering each ("This message was interrupted by a server restart. Send it again?"). Rows newer than the process start are live and left alone; the pass is idempotent (a second start finds nothing new).
- **A write failure is logged and swallowed**, never raised into the drain loop (the `activity_log` pattern).

**In-flight state stays out of the store.** "Sending", "running", streaming text and activities remain on `/v1/turn/{id}` and in `_turns`. A user row carries its `turn_id`, so any client that sees a user row without a reply can poll that turn itself; a reply sent from the phone then streams on the desktop with the same code as the client's own sends.

**The client contract:** a user row is *in flight* if and only if **no assistant row lists its `turn_id` in `answers`**. Match on `answers`, not on the assistant row's own `turn_id`: the two differ for a merged message and for a retry. A restart's interrupted row resolves the rows it orphaned, so nothing stays in flight forever.

### Attachments

`cleanup_attachment` deletes the upload once its turn ends, so each file is copied to `data_dir/transcript-attachments/<attachment id>` when the user row is written: before the agent sees it, and outside `ATTACHMENT_ROOT`, so the agent's read-allow never covers history. **No extension:** the stored `mime` is the only thing that says what the file is. Served by an authenticated endpoint with the stored mime, `X-Content-Type-Options: nosniff` always, and `Content-Disposition: attachment` for anything not `image/*`; the id is validated as a canonical uuid before any path exists; unknown ids and missing files are the same 404.

### API

`GET /v1/transcript` is a cursor read in both directions (`after=<seq>` ascending for catch-up after a push, on foreground and on the poll tick; `before=<seq>` descending for scrollback; neither is the newest page; both is a 422), with a bounded `limit` and `has_more`. Reads have no side effects, so they are safe to retry and safe from two devices. **`/v1/pending` is gone** (step 3, 2026-10-01): nothing read it once the phone moved to the transcript (permission requests come from `/v1/permissions`, reauth from `/v1/health`), so the whole endpoint and the queue's `delivered` flag were deleted.

### Retention: 30 days

Rows (and their attachments) older than 30 days are pruned; clients apply the same cutoff to their cache. This is the "properly persistent" direction `turn-retention.md` punted on; its `_turns` cap stays, for in-flight polling only.

- **Its own task**, `WorkQueue._retention_task`, at startup then daily. The first design said "the same task as `sweep_orphaned_attachments`", but that sweep is a one-shot a test awaits to completion; a daily loop there would hang it, or put the attachment sweep on a schedule it was deliberately not given. The task begins with the restart's interrupted-row pass, then prunes.
- **Batched** (200 rows per transaction, a batch's attachment rows before its message rows, files unlinked after each commit). `post_system_message` writes synchronously on the event loop, so no writer may hold the lock for long; one big `DELETE` over a long-neglected table would freeze the server.

## iOS

The client transcript becomes **a cache of server rows plus a local outbox** (a message not yet accepted stays local, keyed by `client_msg_id`, and the server row replaces it). Sounds and haptics stay keyed on `turn_id`. The notification extension still fetches `/v1/turn/{id}` for the banner text. The cache-first cold launch keeps the 48-hour rule (`TranscriptWindow`) on slow networks. Details and as-built deviations are in `ios-server-transcript.md`. Step 2 went to Opus rather than an implementor: it was the 1,668-line `ChatStore` whose comments are device findings, needing judgment about which still apply.

**Device-gated.** Scroll and launch behaviour changes underneath `ChatView`, and the simulator lies about it. The acceptance test was Mike's phone: a cold launch shows the transcript at once; a briefing appears on both devices; a message sent from the desktop appears on the phone and streams there.

## Not doing

- No server-side "typing" for cron turns; they appear when done.
- No edits or deletes of rows from clients.
- No full-text search (SQLite makes it cheap to add later).
- No migration of phone-local history (Mike, 2026-10-01).
