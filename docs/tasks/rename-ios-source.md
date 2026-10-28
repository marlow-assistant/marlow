# Rename turn source `ios` to `chat`

**Status: MERGED 2026-10-05.** Server writes
`chat`; `active_turn.CHAT_SOURCES = {"chat", "ios"}` is the one place both
spellings are accepted, and `ATTENDED_SOURCES` is that set. iOS
(`TurnSource.isChat`) and web (`isChatSource`) accept both. **Deploy order:
phone build first, then the box** — the old phone build treats `chat` as
unattended in the activity labels and in `ChatStore`'s prefetch path. Side
effect: identity-repo commits on the box now read `turn: chat <id>`.

## The problem

`POST /v1/message` tags every turn `source="ios"` (`server/app.py`), because
the phone was the only client when it was written. What the value actually
means is "a person sent this from a paired device", as opposed to `cron`,
`system` and `compaction`. Once the web client exists, a message typed on the
desktop is stored, logged and shown in the activity log as `ios`.

It is load-bearing, not a label. `source == "ios"` decides:
- **Attended or not:** `active_turn.ATTENDED_SOURCES`. That covers live
  streaming, and whether a permission request goes to a device or is denied
  outright.
- **Coalescing:** `queue._can_coalesce`.
- **Client-chosen effort:** `invoke.py`, in the effort resolver.
- **Transcript user rows, and idle compaction:** `queue.py` and
  `idle_compaction.py`.

## Readers outside the server

Rename them first, because the deployed phone reads the value:
- `ios/.../ActivityFormat.swift` maps `"ios"` to the label "Chat", and treats
  anything else as unattended.
- `ios/.../ChatStore.swift` (around line 1453) branches on
  `turn.source != "ios"`.
- `APIModels.swift` defaults `TranscriptRow.source` to `"ios"`, which only
  matters in tests.
- **Stored data keeps the old value:**
  - transcript rows in `transcript.db`, kept 30 days;
  - activity-log records, persisted and read back by `/v1/activity`.

  So every reader must accept **both** `ios` and `chat` for good, or the
  rename migrates the stored values. Accepting both is the cheaper option, and
  it is safe.

## Order

1. **Clients accept both.** iOS treats `chat` exactly as `ios`. The web
   client should already ignore `source` (`chat-ux-contract.md` §1); check
   that it does. Ship the phone build.
2. **The server writes `chat`.** Change `app.py`, `ATTENDED_SOURCES` and every
   `== "ios"` check. Make the server's own readers of stored records (the
   activity log, and the transcript's restart pass) accept both. Update the
   tests: about 15 files mention `"ios"`, mostly as fixtures.
3. Comments that explain an `ios` check move with it, reworded. Don't drop
   the *why* (CLAUDE.md).

**Fail-closed check:** `ATTENDED_SOURCES` is an allowlist. If the server writes
`chat` before `chat` is in the set, every message becomes unattended: no
streaming, and every tool request is denied without asking. Put both changes in
one commit, and add a test that a `/v1/message` turn is attended.
