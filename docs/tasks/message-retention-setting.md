# Task — a setting for how long messages are kept

**Status:** not started. Specced 2026-10-24 for an implementor session. It
came out of Mike asking how much data multi-image messages pile up,
and when anything cleans it up.

## Decided (Mike, 2026-10-24)

- **History** is a shared preference with four options: 7, 14, **30
  (default)** or 90 days.
- **There is no "forever" and no 1 year.** Marlow remembers through its
  memory files, not the transcript, so a longer history buys only scrollback,
  and the app has no search to make old scrollback useful. A year of messages
  and photos on an always-on box is privacy exposure for an option almost
  nobody would use. Adding 1 year later is one line in each allowed-values
  list, if anyone asks.
- **Both clients** (iOS and `web/`), or it isn't done.

## How retention works today

Every place a message's content is stored, and what removes it:

| Store | Holds | Removed by | Today |
|---|---|---|---|
| `transcript.db` + `data/transcript-attachments/` | every message, plus a copy of each image | `transcript.prune()` at startup and every 24h (`queue.py`, `TRANSCRIPT_PRUNE_INTERVAL_SECONDS`) | 30 days, `RETENTION_SECONDS` in `server/transcript.py`. Deletes whole messages: row, attachment rows and files together. |
| `~/.marlow/attachments/<turn_id>/` | the per-turn upload the CLI reads | `cleanup_attachment` when the turn ends; `sweep_orphaned_attachments` at startup | minutes, or 24h for an orphan |
| CLI session files `~/.claude/projects/<cwd-slug>/*.jsonl` | prompts, replies, and **every image Marlow Read, as base64** | only the CLI's own history cleanup | about 30 days, the CLI's **default**: nothing in this repo sets it. On Mike's box: 92MB, 40 files, oldest 2026-09-23. |
| iOS cache (`chat-transcript.json` + attachment files) | the phone's copy | `ChatStore` load filters at `ChatStore.retention` (30 days); `pruneUnreferencedAttachments` at launch | 30 days, hard-coded |
| Web cache (IndexedDB) | the browser's copy | the `chatStore.ts` load filter at `RETENTION_MS`; `deleteImages` for unreferenced keys | 30 days, hard-coded |
| Activity log (`server/activity_log.py`) | one line per turn: tools used and their allowlisted targets. **No prompt, no reply.** | whole month files past `RETENTION_MONTHS` | 12 months |
| Marlow's memory (`identity/` files, pushed off-box hourly) | whatever Marlow chose to write down | Marlow, or the user asking it to | indefinite, by design |
| `usage.jsonl` telemetry | token counts and timings, no content | — | not a retention concern |

Sessions roll over nightly: `_retire_session` runs after consolidation, so
each session file covers about a day. Nothing in the server reads old session
files, and the CLI only resumes the current one.

## What to build

### 1. Server: the preference

Add it to `server/preferences.py` and `server/app.py`, following
`checkin_every_hours` as the pattern:

- `HISTORY_DAYS_OPTIONS = (7, 14, 30, 90)`, `DEFAULT_HISTORY_DAYS = 30`.
- A `history_days()` accessor that returns the default when nothing is
  stored, or when the stored value isn't one of the options. Never null, like
  `day_start` (the harness runs on it whether or not anyone picked it).
- `PreferencesRequest.history_days: Literal[7, 14, 30, 90] | None`. Add it to
  the "nothing to set" check, to `preferences_store.update(...)`, and to
  `_preferences()`.
- **When the value goes down, prune at once** rather than waiting up to 24h.
  In `set_preferences`, compare before and after, the way the day and
  check-in values are compared, and call the prune off the request thread.
  If a user chose 7 days, the old messages should be gone when they next
  look.

### 2. Server: the transcript follows it

In `server/transcript.py`:

- Replace the `RETENTION_SECONDS` constant with a function of the
  preference, e.g. `retention_seconds()` reading `preferences_store`.
  Update every use: `prune` (the cutoff and its log line),
  `mark_interrupted` (line ~369), and the docstring note near line ~408.
- Keep `prune`'s batching and its delete-the-files-after-the-rows order as
  they are.

### 3. Server: CLI session files follow it, capped at 30 days

These files are the biggest store, mostly base64 photos, and nothing needs
old ones.

- Add a prune for `*.jsonl` in the CLI's project directory for Marlow's
  working directory. Derive the directory the way the CLI encodes the cwd
  (`/home/marlow/marlow-identity` → `-home-marlow-marlow-identity`), and
  put that in one helper with a test.
- Delete files whose mtime is older than `min(history_days, 30)` days.
- **Never delete the current conversation's file:**
  `session_state.conversation_session_id()` names it. If it were deleted,
  the next resume fails with `_DEAD_SESSION_SIGNATURE` in
  `server/invoke.py`. That is recoverable, but it is a cold session for no
  reason.
- Run it from the same daily task as `transcript.prune`, and after a
  lowering in step 1.
- Also set `"cleanupPeriodDays": 30` in `deploy/marlow-settings.json` as a
  backstop, so a change to the CLI's default can never quietly keep these
  files longer. `update.sh` already installs that file. Check that the key is
  allowed at the top level of the CLI's settings schema, and note in the PR
  that it is the CLI's setting.
- The server runs as the `marlow` user, which owns these files. Agent write
  denies on `~/.claude/**` apply to the CLI, not the server, so the server
  can delete them. A test should use a temp dir via settings, not the real
  home.

### 4. Activity log: leave it at 12 months, and say so

This is **Mike's call. Recommendation: leave it.** It keeps no prompts or
replies, only which tools ran and their allowlisted targets. It is the
product's record of what Marlow did, and month-file granularity makes
"follow a 7-day setting" awkward. Instead, the setting's footer says what
the setting covers (see step 6). If Mike decides otherwise: make
`RETENTION_MONTHS` follow `ceil(history_days / 30) + 1` whole months, and
don't rewrite month files in place.

### 5. Clients: follow the preference, cap the local copy at 30 days

The clients already fetch `/v1/preferences`. Read `history_days` from it.

- **The local cache keeps `min(history_days, 30)` days.** At 90, older
  messages stay on the server and "Show earlier" (`hasOlderOnServer`)
  fetches them. Keep that path working, because a 90-day user depends on it.
  The 30-day cap is deliberate: the iOS cache is one JSON file rewritten on
  every event (`docs/tasks/ios-transcript-scaling.md`, persistence half still
  open), so a 90-day local copy would be three times that file for no gain.
- Store the last value seen, so a cold launch offline still applies it.
  Default to 30 when nothing has ever been seen.
- When the fetched value drops below the current local window, apply it at
  once: drop the rows, then delete unreferenced images.
- **iOS:** replace `ChatStore.retention` (a `static let`) with a value the
  store holds and updates from preferences. Keep the filter rule
  `seq == nil || createdAt >= cutoff`: local-only unsent messages are never
  dropped.
- **Web:** the same for `RETENTION_MS` in `web/src/lib/chatStore.ts`, keeping
  the same filter rule. Update `chatStore.sync.test.ts`, which imports
  `RETENTION_MS`.

### 6. Settings UI, both clients

- **iOS** `SettingsView`, **web** `web/src/app/Settings.tsx`. Add a "Keep
  messages for" picker with 7 days / 2 weeks / 30 days / 90 days. Put it near
  the day and check-in preferences. `docs/tasks/settings-overhaul.md` is
  reorganising this screen but hasn't started, so don't wait for it, and add
  a line there that this row exists.
- **Shortening asks first:** "Messages older than 7 days will be deleted from
  Marlow and your devices." Use the wording from `facts-on-server`: the
  server sends numbers, and the client words them.
- **Footer** (the honest scope): "Covers chat messages and photos. Marlow's
  memory keeps what it chose to remember. Ask it to forget something to
  remove it."

### 7. Identity

Add one line to `identity/CLAUDE.md`, in the spirit of its "Updates" section:
chat history is kept for the period set in Settings and then deleted; its own
memory files are separate and are not affected. Then, if asked whether it
keeps everything, Marlow answers truthfully instead of guessing.
`update.sh` installs it.

## Tests

- **pytest:**
  - the preference: options accepted, others 422, the default when unset or
    corrupt;
  - `transcript.prune` uses the preference: rows just inside and just outside
    a 7-day window, with attachment files removed with their rows;
  - lowering the setting prunes at once;
  - session-file prune: deletes old files, keeps new ones, **never the
    current conversation's file even when it is old**, caps at 30 at
    `history_days=90`;
  - `cleanupPeriodDays` is present in `deploy/marlow-settings.json`.
- **Swift Testing:** the load filter at 7 and at 90 (90 keeps 30 locally);
  local-only rows survive; the value persists and is used offline; a lowered
  value applied at runtime drops rows and prunes their images.
- **vitest:** the same set for the web store.
- **Playwright:** the settings picker posts `history_days`, and the shorten
  confirm appears.
- Run `xcodegen generate` if you add Swift files, and check that the Swift
  test count moved. For each new behaviour, break it and watch its test fail
  before trusting it.

## Docs and housekeeping (same PR)

- `docs/chat-ux-contract.md`: add the retention rule wherever transcript sync
  is described (search for "30 days"), now "the History preference, local
  copy at most 30 days".
- `changes.json`: one `feature` entry ("Choose how long Marlow keeps your
  messages: 7 days to 90."). Read `_readme` for the next id.
- `server/transcript.py`'s module docstring, where it says 30 days.

## Not in scope

- Search across history.
- Retention for Marlow's memory, or a "forget everything" button. Both are
  separate decisions.
- Retention longer than 90 days.

## Done means

- All local suites pass, with counts quoted (pytest, Swift Testing, vitest,
  Playwright).
- The picker checked in the iOS simulator (`-uiFixture idle` disables
  Settings writes, so use the real Settings screen against a local server or
  read the code path) and in the web client at pixel7 size.
- `/code-review high`, plus `/security-review`: this deletes files under the
  CLI's home, and the session-prune path must not be steerable by anything a
  client sends.
- Not checked until the box runs it: a real prune of session files on Mike's
  box, and its log line. Say so in the PR. Don't open the PR without asking
  Mike.
- Repo rules: name no person but Mike, and name no other AI assistants.
