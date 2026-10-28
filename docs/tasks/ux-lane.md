# UX lane — polish, activity log, transparency

**Status (2026-09-29):** all three steps merged.
Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

1. **Polish** — merged 2026-09-28, device pass done.
2. **Activity log** — merged 2026-09-28. Device pass: a phone turn with
   approvals and a decline, and a cron turn on stream-json, both recorded
   correctly on the box. Not yet observed: a morning briefing's connector calls
   in the log.
3. **Transparency** — merged 2026-09-29. Device pass done on Mike's iPhone.
   Parked follow-ups are at the end of §3a.

Outside the three: **failed-turn fixes**, merged 2026-09-28; see "Parked,
related" for what is still unobserved.

**Naming rule:** these came out of looking at a competitor's agent app. Never
name it in anything committed here — commits, PR titles and bodies, branch
names, docs, code comments. The repo may go open-source. Describe each change
by the need it serves. (Marlow's private backlog on the box can name it.)

**The one-line thesis:** Marlow's architecture already matches the best of
what is out there (SOUL, curated memory, a nightly tidy, cron jobs,
per-turn commits); the gap is that none of it is *visible* in the app. These
three steps put it on screen.

## Decisions already made — do not re-open

Mike, 2026-09-28:

- **Settings stays on the gear**, a round floating button top-right.
- **Tapping the avatar opens the transparency view** — recent actions, SOUL,
  memory. In step 1 the avatar does nothing yet.
- **No "typing" dots.** Most of a turn's wait is tools and thinking, not
  writing; the working state is a spinner/animation, not a fake "Marlow is
  typing".
- **Assistant bubbles stay off** (they were removed in the 2026-09-08 UX
  affordances change to win back width).
- **Dark mode is not forced.** Low priority: get Mike to try the app in
  light mode once the polish settles — it has never been looked at.
- **The app icon is the avatar** for now.

## 1. Polish

Shipped: a floating presence header replacing the nav bar (avatar, a status
pill, a pulsing ring, and the `Sonnet/Low` chip top-left balancing the gear —
it was in the pill until the device pass, lopsided there and hidden mid-turn),
a pill composer, the icon's monocle cyan as the accent, and a greeting overlay
on an empty chat. A small accent arc at the foot of a working reply replaced the
in-row spinner and label, and `ChatPresence` chooses which turn the header shows
(the oldest in flight).

Device pass (Mike's iPhone, 2026-09-28) found nothing but the pill
overlapping the avatar ring; "Show earlier messages" did not park the row
under the header, so that review finding is closed.

## 2. Activity log

**What:** a list of what Marlow has done, one entry per turn — like
"Morning briefing · read Gmail, Calendar, Trello · edited travel.md · 6:02am".
Pairs with the pre-approval sheet: that one is *before*, this is *after*.

**How — deterministic, no LLM.** The server already sees every tool call's
name and input (stream-json events), approvals and denials (the permission
round trip, `permission_denials`), the files each turn changed
(`identity_git.commit_after_turn` commits every turn), and the turn's source
and timing. The real work was persistence: `Turn.activities` lived in memory
only, capped at 64 events per turn and 256 turns, and wiped by a restart.

**Why not model-written titles** ("Checked avatar and found static image
only"): a prose summary needs a second model call per turn — through the one
work queue, on the subscription — and it is a claim by the model about
itself. `dated-plans-go-stale.md` found two invented self-diagnoses in
Marlow's memory. The mechanical record is true by construction. Prose titles
could come later from the nightly consolidate job; start mechanical.

**Security:** new data exposed over the API, so `/security-review` as well as
`/code-review`. Mind `SECRET_GLOBS` for file names and tool inputs that could
carry secrets.

### 2a. Persistence — design settled 2026-09-28 (Mike)

**What was missing.** Cron turns ran `--output-format json`, which has no
tool blocks, so `Turn.activities` was empty for exactly the morning-brief
case (`invoke.py`'s `streaming` gate). `commit_after_turn` computed the
changed paths and returned only the sha. `Turn` had no job name — `source`
is just `"cron"`.

**Every logged turn runs `stream-json`.** Attended turns keep
`--include-partial-messages` and the live callbacks; unattended ones get
plain `stream-json --verbose` and are collected, not streamed to anyone.
Compaction is not logged and stays on plain json, the format its probe ran
against (`invoke.run_claude`'s `on_tool_call`).
This reverses the decision rule in `streaming-reply-text.md` for a new
reason: the log must show a cron turn's tools even though most cron turns
call none (73 of 75 when last measured). `_final_envelope` has parsed
stream-json on every attended turn since August. Rejected: reading the CLI's
session `.jsonl` after the turn — undocumented format, and a dead-session
retry lands in a different file.

**What a tool call records.** `ToolCall(name, target, at)`, collected beside
`ActivityEvent` (which stays name-only: it feeds the live wire and §1.2 of
`turn-activity-streaming.md` still governs it). `target` comes from a named
per-tool allowlist, the review §1.2 asked for: a file path relative to the
working dir for Read/Edit/Write/NotebookEdit (none if secret or agent-config),
the query for WebSearch, the host only for WebFetch (URLs carry tokens), and
nothing for Bash, Grep, Glob or any MCP tool. Targets are capped at ~120 chars
and a turn keeps its first ~100 calls. The app maps MCP names to service names
("Calendar"); the server stores the raw name.

**The record** is one JSON line per turn, written in `_drain` after
`commit_after_turn`, off the loop and guarded like it (a failure to write
must never kill the drain task): when, source, label (cron job or schedules
entry id), duration, tools with targets, approvals and denials, files changed,
commit.
- **No prompt, no reply text.** The chat holds those; `turn_id` links them.
- Skipped: compaction turns, system notices, CANCELLED. A coalesced run is one
  record naming the turns it absorbed.
- The changed paths are already filtered of `SECRET_GLOBS` and
  `AGENT_CONFIG_PATHS`. Denials are recorded by tool name only.

**On disk:** `data_dir/activity/YYYY-MM.jsonl`. `data_dir` is in
`SECRET_PATHS`, so the agent can neither read nor write it — Marlow cannot
edit its own record, which is what "true by construction" needs. Each record
is one `O_APPEND` write; the reader skips an unparseable line (a torn tail
after a crash). **Retention: 12 months** (Mike), enforced by deleting whole
month files at write time — no rewrite ever.

### 2b. Read endpoint — built 2026-09-28

`GET /v1/activity?limit=50&before=<finished_at>`, device-token gated.
Newest first; `next_before` is set only when the page came back full, so the
client stops on a null. Pages by timestamp, not offset, so a turn finishing
mid-scroll cannot shift the page. Records go out as written — the writer is
the privacy boundary, so the endpoint strips nothing.

### 2c. The screen — built 2026-09-28, wording to tune after use

**Entry: the avatar opens an Activity pane** (Mike, 2026-09-28) — a stopgap
until step 3's transparency view took the tap and held this list as one part.

**A row:** title ("Morning briefing", "Chat", a humanised schedules id),
time, one summary line built by `ActivityFormat` ("Checked Gmail, Calendar ·
read travel.md · changed travel.md") and an orange flags line when something
failed or was blocked. Tap expands to one line per tool. Grouped Today /
Yesterday / date. Nothing is cached on the phone.

- **Tool names are never shown**, the rule `TurnActivity.label` already
  follows: MCP names map to a known service or count as "another tool".
- **Chat turns that used no tools are hidden** — the conversation already
  shows them. Every cron turn shows, idle ones included ("did the morning
  job run" is half the point). This is the first thing to revisit if the
  list feels thin or noisy.

Mike wants to live with the wording for a few days before tuning it.

## 3. Transparency

**What:** make Marlow's configuration and standing behaviour visible in the
app, behind the avatar: schedules (a read-only list rendered from
`cron/schedules.json`), identity files (SOUL, USER, memory as cards that open
into rendered markdown, each with a short **"About this file"** note saying
what the file is, that Marlow reads it every turn, and that it can be out of
date — the note is not part of the file), and the recent-actions list from
step 2.

**Read-only first.** Editing from the phone means a write endpoint for files
injected into every prompt. Only Mike could call it, but it is the
permission/credentials surface, so it is a deliberate second step with its
own `/security-review`.

**Identity update in the same PR** (CLAUDE.md rule): Marlow's identity — both
`identity/CLAUDE.md` here and the live one on the box — had to learn that Mike
can now see its memory and schedules from the app.

### 3a. Design — shipped 2026-09-29

**Shape (Mike, 2026-09-29: an icon strip, not one long list).** The avatar
opens one sheet, `MarlowView`, replacing the Activity stopgap (a permission
request still pre-empts it). Under the avatar, a row of five labelled icons;
the selected one takes the accent, the panel below switches. The icons hint at
what each panel holds, so the whole of Marlow's standing state is visible at a
glance even unopened. Activity is selected on open.

The five panels: Activity (the list above), Schedule (grouped by kind, below),
Memory (one row per top-level `memory/*.md`: name, the index description the
prompt carries, last-changed date), You (USER.md rendered in place) and Soul
(SOUL.md rendered in place, then a row pushing CLAUDE.md as "Instructions").

A file screen shows an "About this file" note (one fixed sentence per kind,
written in the app), then the file rendered with the existing markdown parser,
frontmatter stripped. Nothing is cached on the phone.

**Server — two read-only endpoints** in `server/transparency.py`, device-token
gated: `GET /v1/identity` (the file index plus the schedules, one call for the
whole sheet) and `GET /v1/identity/file?id=` (text capped at 256 KB; the largest file today is 42 KB).

**The security boundary is the enumeration.** The file endpoint looks `id`
up in the same list the index builds — SOUL/USER/CLAUDE paths from
`settings`, `memory_dir`'s top-level regular `*.md` files, and
`cron/<job>.md` for each `job` in a valid store. The id is never joined onto
a path. On top of that, anything `is_secret_path` or under
`AGENT_CONFIG_PATHS` is dropped, and a symlink is dropped (resolve and
require the result still sit inside its directory). Unknown id → 404, same
body for "doesn't exist" and "not allowed". Tests pin: `../`, absolute paths,
a symlink out of `memory/`, a `.env` in `memory/`, a subdirectory file. The
data is Mike's own and already flows through chat, but it is a new read
surface over the whole identity, so it got `/security-review`.

**Schedules.** `rejected` carries the validator's reason when the store is
invalid, and the app shows "Marlow's schedule file has an error; the last good
version is still running" plus the reason, since that is exactly the state
nobody would otherwise see. `next_fire_at` steps days forward in the store's
zone, cron-style (day-of-month/day-of-week OR'd when both are restricted), and
skips a wall-clock time that doesn't exist on a DST-skip day rather than
guessing.

**Facts on the server, wording on the phone** (Mike, 2026-09-29: a UX tweak
must not need a box deploy). `recurrence` is structure, never a sentence:
`daily` with `times`, `weekly` adds `weekdays` (0–6, Sunday 0), `monthly` adds
`days`, an `at` entry is `once`, and anything else — a month restriction, both
day fields, a stepped day field — is `other`, shown from the raw `cron`. The
server keeps the parse because it owns the validator and the day-field rules
are what a client would get wrong. A first build sent a `cadence` sentence from
the server; it was removed for this reason.

**Schedule panel** (Mike, 2026-09-29: the question is "what is set up, does
it look right?", an audit — not "what is Marlow about to do?"). Sections, empty
ones hidden: Reminders (every `notify` entry, soonest first, orange once
overdue), Daily / Weekly / Monthly (by `recurrence.type`, one line each with the
time on the right), One-off jobs, and Other (the raw `cron`). No "Next:" line:
the section says the cadence and the right-hand text the time. Job rows push
their instructions file.

**Out of v1 (parked, one line each):**
- *Last run per schedule* ("ran 8:01 · ok") — the activity log has it by
  `label`; add when the list feels like it's missing it.
- *Memory subfolders* — not injected into the prompt, so not "what Marlow
  reads every turn"; add a folder row if Mike reaches for them.
- *Editing* — §3's second step, its own `/security-review`. Deliberately
  "later" (Mike, 2026-09-29: visibility is 90% of the value); wait for a
  felt need, e.g. Mike repeatedly asking Marlow to fix a line he can see.

**Identity update.** One bullet at the end of `## Memory` in
`identity/CLAUDE.md`, and the box copy on 2026-09-29 once the screen was on
the phone: Mike can read SOUL, USER, the instructions file, every top-level
memory file, the schedules and job instructions in the app, read-only, and a
memory file's `description:` is the subtitle he sees. So memory is written for
him to read too: current, plain, nothing stale left standing.

## Parked, related

- **Failed-turn fixes — shipped 2026-09-28.** From an API 522
  that killed a chat turn: retry now re-runs a turn that ERRORed (the
  `client_msg_id` dedupe used to hand the failure back); the CLI's
  `API Error: <status>` is mapped to a fixed sentence in `turn.error` and the
  journal, never quoted; the retry arrow sits beside Mike's failed bubble.
  Device pass: layout and retry-after-a-killed-turn both OK. **Never
  observed:** the reason text from a real API failure — at the next outage
  the journal should read `claude -p exited 1 (Anthropic's API timed out —
  try again.)`. **Known gap, left alone:** retrying a failed *coalesced*
  burst resends only its last message.

- **Standing "watch" jobs** ("watch this listing and tell me if it
  changes"). Smaller than it sounds: cron turns already run unattended, so
  the gap is state carried between runs, notify-only-on-change, and an end
  condition — built on the schedules store. Needs a design doc with the
  blast-radius question up front. Not in this lane.
- **Marlow for people other than Mike** (OSS self-host / hosted,
  bring-your-own-key). Reopens the CLI-not-SDK constraint; Mike's call, not
  this lane. Detail lives in the box's `memory/marlow-for-others.md`.
- **First-run onboarding** — built on `first-run-onboarding` (2026-10-02); see `friends-alpha.md` item 9.
- **Tap-to-answer options** (Mike, 2026-10-02, from the onboarding VM runs):
  when Marlow asks a question, show 2–3 likely answers as buttons under the
  reply; a tap sends that text as an ordinary message. No CLI work needed. A
  sketch: the identity tells the model to end such a reply with a fixed
  trailer line listing the options; the server strips it from the reply
  text (stream included, or old clients show the raw syntax) and stores the
  options on the turn and its transcript row. Clients draw buttons on the
  latest assistant message only, and drop them once anything is sent.
  The open risk is the model using the trailer consistently, and the stream
  showing it before the server can strip it. Onboarding is the obvious
  first user (briefing time, "want a reminder?").
- **Replies animating in** when they finish. Streaming already types them
  out, so Mike is fine without it for now; ask again once he has lived with
  the new header for a few days.
- **Parakeet dictation** — parked 2026-09-28, see
  `ios-modern-on-device-dictation.md`.

When a step here ships, mark it on the box's `memory/marlow-features.md` too
(the "Show operating instructions in-app" and "Recent-actions log" entries),
or the two lists drift.
