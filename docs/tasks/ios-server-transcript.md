# iOS: the transcript comes from the server

**Status: shipped.** Merged 2026-10-01 and device-checked on Mike's phone (362 Swift tests at merge). Step 2 of `server-transcript.md`; the server half merged just before it. Reviewed once; the review's three findings were fixed before merge.

Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

Mike's calls, 2026-10-01: iOS switches to the server transcript; old phone-local history may be lost; 30-day retention; Sonnet implements and Opus reviews.

## The shape, and the principle: local identity wins

`ChatView` renders `ChatStore.messages`, and nearly all its hard-won scroll behaviour (`ReplyFollow`, the jump button, the unseen-arrival dot) depends on **message identity staying stable**. So the view and `ChatMessage`'s role don't change; `messages` becomes **a cache of server rows plus local-only messages**, merged by one pure function (`TranscriptMerge`).

**Local identity wins.** A message this phone created (a sent user bubble, its assistant placeholder) keeps its local `id` for life, and the server row that confirms it is folded into it. Only rows with no local counterpart get a new `ChatMessage` (id = the server row's uuid). Never swap a visible message's `id`: that is a row teardown and breaks follow-scroll mid-stream.

The merge rules, which the web client ports (`web/src/lib/transcriptMerge.ts`, contract SYN-3/SYN-4), processing rows in ascending `seq` and skipping any `seq` already present (so re-applying a page is idempotent):

- **User row:** matches this phone's own send by `client_msg_id` (set `seq`, keep the local image); otherwise insert a new user message, and if no assistant row (in the batch or already present) answers its `turn_id`, add a running placeholder and start a poll. That is how a reply sent from another device streams here.
- **Assistant row:** resolve the placeholder by `turn_id`, else by `answers`; any *other* placeholder named in `answers` is a merged turn and is removed (what `pollLoop` does on a `merged` status). If nothing matched, insert a new message. An empty error text shows "Something went wrong."
- **Placement:** immediately after the last message whose `seq` is below the row's, local-only messages staying after that point (a cron briefing lands above this phone's pending send, in server order). **Exception (review, 2026-10-01):** a live placeholder directly after that point stays *above* the insert, so a confirmed send keeps its streaming reply directly under it.
- **`announce`** is false for the bootstrap fetch so a fresh install doesn't play 100 receive sounds.
- A placeholder that reaches `done` from its own poll before its row arrives is matched by `turnId` and just gets its `seq`; `completedTurnIds` must not fire for a placeholder already `.done`, or the receive sound plays twice.
- Decoding is lenient: unknown `role`/`status`/keys must not fail the page. An unknown `role` decodes as `.other` and the merge skips it (its `seq` still counts); an unknown `status` is `.error`. `client_msg_id` is matched as a `UUID`, so letter case does not matter. `answers` is stored on `ChatMessage` and persisted with the cache, so already-merged messages can be matched.
- The old cache is deleted and the new one (`chat-transcript-v2.json`) starts empty (Mike's call); `chat-attachments/` is left alone by that deletion, because this phone's own images live there. (Orphan cleanup in that directory on load is a separate thing, below.)

## What was built (deviations from the first design)

- **Cursor.** The server's `latest_seq` is the table's newest `seq`, not the page's last row, so the catch-up loop moves the cursor to the last row's own `seq` and stops if a page makes no progress whatever `has_more` says. The bootstrap sets it to the highest row `seq` in the page (the page's `latest_seq` only for an empty page), so a row written between the server's two reads is not skipped.
- **Concurrent syncs wait, not skip.** A tap on a push cancels the health loop and starts a successor in the same turn while the old sync may still be unwinding; skipping would drop the sync the tap exists for. Each sync checks `Task.isCancelled` after its fetch and applies nothing if cancelled: a transcript read consumes nothing, so the next sync loses nothing (unlike the old `/v1/pending` drain). Sync runs **before** `refreshHealth()` in the loop: after a push wake, the transcript is what the user is waiting for.
- **Windowing.** After a bootstrap the launch window is recomputed so a fresh install doesn't render every fetched row; after a "Show earlier" server page the store reveals one window step, not the whole page. `hasOlderOnServer` is persisted.
- **Joining mid-stream.** A placeholder for another device's turn is inserted into `resumedPlaceholders` so it catches up rather than typing the whole reply out (see `streaming-reply-text.md`).
- **Retry clears the confirmation.** `retry()` (and the `.sending` resend in `reconcilePendingMessages`) clears the placeholder's `seq` and `answers`. An error row confirms a placeholder; if it kept that `seq`, the retry's reply row (turn B, answering A) could not match it and would land as a second bubble while the poll also completed: two replies, two sounds (review, 2026-10-01).
- **Attachments.** Besides downloading on a row, `init` retries any message with an `attachmentId` and no `imageFilename`, once per launch (not every tick). On load, after the 30-day cutoff, files in `chat-attachments/` that no kept message references are deleted off the main actor (the 30-day cutoff itself never drops a local-only message), except anything modified at or after launch (a first send or download may be writing one concurrently).
- **Left alone on purpose.** `APIClient.getPending` stayed for one release (since removed with the endpoint). A server `transcript.db` reset is not handled: the phone's cursor would sit above the server's `latest_seq`. Parked until it is a felt symptom.

## Device pass, 2026-10-01 (Mike's phone)

- **Cold launch:** messages loaded and scrolled to the newest, but the empty-chat greeting showed first (that launch had just deleted the old cache). Fixed: `awaitingFirstFetch` turns the greeting's second line into "Loading your conversation…" with a spinner. Mike's call: the greeting is fine if it is clear a fetch is under way.
- **Send and stream:** streamed and follow-scrolled; felt fast. Not confirmed as real streaming with a long reply.
- **Push with the app closed:** passed; the lock-screen banner carried the full text.
- **Another device:** passed, with a curl script pairing a second device and posting. An old Mac install's notification extension also showed it.
- **"Show earlier" past the first page:** **never observed**; the server held fewer than 200 rows. **Open:** `ChatView.revealEarlierMessages` scrolls to its anchor one run-loop turn after the call, right for the local reveal but for a server page the rows arrive later and may push the reader down. The fix would be a small `ChatView` change that scrolls after the fetch completes.
- **Airplane resend:** one bubble, one reply. The send sat on its spinner with "Can't reach Marlow" rather than failing; that is the send policy's deliberate ~3.5-minute retry budget, unchanged.
