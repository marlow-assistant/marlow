# Onboarding v2: a shorter, structured first run

**Status:** wording pass merged 2026-10-18; harness phase 1 (chunks 1–5
below, server, identity and the activity label on both clients) merged
2026-10-31, after four first runs on the trial VM. The last was good; the
earlier ones led to asking before looking at mail and calendar, one question
per message, one step per reply, and a tour with one thing to try. Not yet
seen with a real new user. The pass, ahead of the next user onboarding: new greeting; one
reminder, then the Schedule, then stop; reminder vs note explained; "things to
try" as its own early message; the briefing and week-ahead asked about, the
inbox check told not asked and shown as "Check-in" on both clients; USER.md and
the first approval explained; detours return to the step. Still open: the
harness below, Drive, dictation. Source: Taylor's first run on her
own instance, 2026-10-17, talked through with Mike afterwards. She is the first
user who neither built Marlow nor watched it being built, so this is the
first outside read of `identity/FIRST-RUN.md` and the greeting
(`server/app.py` `FIRST_RUN_GREETING`).

## What happened

1. Greeting: "I run on your own machine, reach you here…", then "what should I
   call you?"
2. Name, then a time-zone question: Marlow saw that her location didn't match
   the install's time zone and asked what to do about it.
3. After that it stopped. The greeting had promised "a few minutes" of setup,
   but no step-2 question came. Taylor had to ask "what's next for the setup?"
4. Marlow then looked through Gmail and Calendar, said what it could see, and
   asked what to stay on top of: "bills, appointments, a trip, a project or
   something recurring like your bins. Name one and I'll set it up now, either
   as a reminder or a note I keep watching."
5. Setup grew complicated fast: the morning briefing, inbox check and week
   ahead were presented as already set up, with no ask first.
6. An approval prompt came up. Taylor approved without knowing what it was.
   Later Marlow said "I'm saving what I've learned about you to USER.md, please
   approve it", which didn't explain it either.
7. About 20 minutes in, after she had already asked it to remember something
   and set reminders, Marlow closed with "things to try next": send a photo of
   a letter or a receipt, tell me something to remember, ask for a reminder.

## The feedback, by theme

### The script lost its place (the main finding)
- The time-zone anomaly derailed it. Asking about it was reasonable (Taylor:
  reminders depend on it), but it shouldn't come first, and afterwards Marlow
  forgot the rest of the setup.
- Today the whole of FIRST-RUN.md is one set of instructions handed to the
  agent once, then free conversation. Nothing holds it to the sequence, so the
  first detour ends onboarding. Mike: "We dump a bunch of instructions on
  Marlow and expect it to follow them."

### Explain before doing
- Before it touches her mail or calendar, say what Marlow can do: reminders,
  notes, lists, finding things in email and calendar.
- Explain the difference between a reminder and a note. The step-4 question
  used both words with no explanation.
- Explain approvals the first time one appears: what the prompt is, why
  Marlow asks, and what USER.md is. Today's line (FIRST-RUN step 7) assumes
  the user knows what USER.md is. Taylor only knew from Mike explaining it
  elsewhere.

### One thing at a time, and ask before setting up
- Taylor's shape: set up one reminder together, then take the user to the
  Schedule to see where it lives. Then stop. (She corrected herself on
  "one reminder and one note" down to one reminder.)
- Morning briefing, inbox check and week ahead: ask whether she wants them
  before setting them up. Fact check: install.sh seeds all three, and
  FIRST-RUN step 5 only tells the user about them and asks whether the times
  suit. To Taylor that read as "it set them up for me". The fix may be the
  wording, opt-in seeding, or both.
- The inbox check is different (Mike, 2026-10-17): don't ask about it at
  all. It is what keeps Marlow relevant through the day, and other assistants
  simply watch the inbox without asking. Asking makes an internal job look
  like a setting. It also needs a user-facing name: "email flash" is ours, and
  something like "check-in" says what the user gets. It stays the job's
  internal name (`cron/email-flash.md`); only what the user sees changes.

### "Things to try next" is misplaced
- It should be its own message and come earlier, not tacked onto the wrap-up.
- By then it was redundant: she had already done two of the three.
- "Send a photo of a letter or a receipt" came with no reason. Say why it's
  useful, or drop it.

### Greeting wording
- "I run on your own machine" means nothing to a non-technical user. Taylor:
  what machine? Nothing of hers was nearby. Her instance runs on Mike's box,
  so it isn't her machine either. Needs rewording for someone who doesn't know
  what a server is.

### Google Drive isn't reachable (a capability gap, not wording)
- Taylor connected Drive alongside Gmail and Calendar at claude.ai. Marlow
  doesn't use it: it could open a Drive link she pasted, but not through the
  connector she approved. Her expectation: if Gmail and Calendar just work, so
  should Drive.
- Probed 2026-10-17 as `taylor`, CLI 2.1.302 (`scripts/probe-drive-connector.sh`):
  `claude mcp list` and the session's init both show Drive **connected**, but
  it contributes **no tools**. Even `ToolSearch` for the exact name
  (`select:mcp__claude_ai_Google_Drive__list_recent_files`) finds nothing,
  while Gmail's lookup on the same run works. So it isn't the identity or the
  allowlist, not yet: the CLI gets an empty tool list from Drive. Not a general
  Drive failure either: a Claude Code desktop session on Mike's account has
  the Drive tools. Re-run as `marlow` (Mike's account, CLI 2.1.301), now with
  Google split into four connectors (Drive, Docs, Sheets, Slides): the same.
  Docs, Sheets and Slides contribute tools; Drive contributes none. So it is
  the Drive server or the CLI, not Taylor's grant and not our code. Re-check
  after a CLI update; nothing for us to build until Drive tools load.
- Once tools load: tell the identity about Drive, and pick which Drive read
  tools join the `deploy/marlow-settings.json` allowlist (none do today, so
  every Drive call would ask the phone).

### Dictation looked like send-only (both clients)
- The mic in the message bar fills the text field, but Taylor thought the
  blue send arrow was her only next step and didn't realize she could review
  and edit the text first. The two controls need to look like two different
  actions. Could also be part of a walkthrough. Check `web/` too.

## Direction (Mike and Taylor)
- More structured and somewhat shorter. Possibly rigid and scripted, "like a
  video game tutorial": you can't get past a step until you've done it.
- Smaller units: give Marlow one step at a time and have it report when that
  step is done, instead of the whole script up front. The server would hold
  the user's place in onboarding rather than the agent.
- Whatever is built needs iOS and `web/`.

## Harness design (agreed with Mike 2026-10-30, not built)

The model isn't untrustworthy here, it gets sidetracked. It knows when a step
is finished; what it lost with Taylor was where it was. So the server keeps
the place, the model reports when a step is finished, and where a step leaves
something on disk the server checks that too.

### The pieces

1. **A step line on every turn.** `server/turn_context.py` already adds a
   `[Context: …]` line. During onboarding the server adds one more, naming the
   current step and its instructions. It repeats until the step ends, so a step
   can take several turns and a detour (the time-zone question) can't end
   onboarding: the next turn names the same step. The line also says a real
   request comes first: help with it, then come back.
2. **An onboarding tool the model calls when a step ends.**
   `mcp__onboarding__step_done(step, outcome, note)`: `outcome` is `done` or
   `skipped` (they waved it off), `note` a line on what was learned. Its
   **result is the next step's instructions**, so the model carries straight
   on in the same reply. This is the main reason for a tool over a file write:
   with a file, the next step only arrives on the next turn's line, so the
   model finishes step 1 with nothing to ask next, which is Taylor's "it just
   stopped" rebuilt. **One step per turn**: the reply opens the next step
   with its question and stops. A second `step_done` in the same turn gets
   "Not yet" from the tool (one process per turn, so it remembers), and the
   record advances at most once a turn whatever the model calls. A trial-VM
   run without this limit (2026-10-30) chained steps the user never answered
   and finished setup in four messages.
3. **The server decides.** After the turn it reads the tool calls out of the
   stream and moves the user on only if the step's disk check passes too. A
   claim the check rejects leaves the step open, and the next step line says
   what's missing ("no reminder has been saved yet"). The model can run ahead
   of the server for one turn at most; the step line pulls it back.
4. **A turn cap per step.** After N user turns on one step, the server moves
   on and the next line says so ("move on from X; they can come back to it").
   Only chat turns that finished count, never cron or system turns, and never
   one that errored (the CLI down shouldn't use up a step). A real request in the
   middle of onboarding counts too: a count the model has to report is more
   machinery than a slightly higher cap.

### Alternatives considered for the done signal

- **A tag in the reply text.** Replies stream to both clients
  (`PartialTextTracker`, `--include-partial-messages`), so the tag would show
  before the server could strip it, unless the streaming path learned to hold
  back tag-shaped text.
- **A file write** (`onboarding/done`). It works, but it needs a new write path
  in `server/file_policy.py` (only `memory/`, `cron/schedules.json` and
  `ideas/ideas.json` write without asking the phone), the file lands in every
  identity commit, and it can't hand over the next step mid-turn.
- **Only disk checks.** Conversational steps leave nothing on disk, so they
  would advance on "the user replied", which is wrong for a step that takes
  several exchanges.

### The steps

Today's eight steps, regrouped so the ones with a result on disk are checked
against it. Each turn gets the current step's instructions, not the script.

| Step id | What | Disk check (besides the tool call) | Cap |
|---|---|---|---|
| `name` | what to call them | none | 3 |
| `stay-on-top` | look at mail and calendar first, then ask what to stay on top of; reminder vs note | none | 6 |
| `one-thing` | set up one thing while they watch | some onboarding turn's commit touched `cron/schedules.json` or a path under `memory/` (sticky: saved in one turn, claimed in the next, still counts) | 4 |
| `tour` | where it lives (avatar → Schedule), then one thing to try, and a response to it | none | 4 |
| `runs-alone` | briefing and week-ahead asked about, check-in told | none | 4 |
| `wrap-up` | up to two ideas, then USER.md, approval explained first | USER.md no longer contains "Not yet known" | 4 |

`skipped` passes any step without its disk check. The caps are a first guess
to tune from real runs. If `wrap-up` runs out with USER.md unwritten,
onboarding ends anyway and the record says so; USER.md keeps the placeholder
until normal use fills it, and nothing keys on it any more but the check.

### State and the existing boxes

- **The record** is server-owned, `onboarding.json` in the data dir (hidden
  from the agent by the read denies and bwrap): current step id, chat turns on
  it, each step's outcome, whether anything has been saved yet, `finished`
  and how it ended (`done`, `capped`, or `existing` for a box that onboarded
  before this). Written only by the server's post-turn step. The tool's `note`
  is not kept: it is what the model learned about them, it belongs in USER.md,
  and asking for it is enough to make the model sum the step up. The activity
  log keeps step and outcome only (`invoke._tool_target`).
- **Active or not:** no record and USER.md still has the placeholder starts
  onboarding (record created at step `name`). A record says whether it is
  finished. No record and no placeholder is a box that finished before this
  shipped: finished, written as such the first time it is checked, so no
  existing user ever sees a step line.
- **It replaces the text search.** `changes.onboarding_complete` reads the
  record (the loose end in the START doc). It still fails open.

### What changes in the identity

`identity/FIRST-RUN.md` becomes the step file: a `## <step-id>` section per
step, each with its instructions, nothing outside the sections except a
one-paragraph header comment for authors. It stays product-owned (edit-denied,
installed by `update.sh`). The cross-cutting rules (lead each step, short
messages, detours return to the step, explain the first approval) move into
the fixed wording of the step line. `identity/CLAUDE.md`'s "Read FIRST-RUN.md
and follow it" becomes: onboarding arrives one step at a time on the step
line; call the tool when a step ends. Plus a `changes.json` entry. All in the
same PR.

### Implementation chunks

Feasible to hand to the `implementor` agent for chunks 1, 2 and 5. Chunks 3
and 4 stay with the orchestrating session: 3 changes what `invoke.py` passes
the CLI and the permission surface, 4 is wording. Order: 1, 2, 3, 4, 5, then
`/code-review high` and `/security-review` (the tool surface changes). Write
each test first and watch it fail.

**Chunk 1: `server/onboarding.py`, the state machine.** No I/O beyond the
record and USER.md; no `invoke`/`queue` imports.
- `STEPS`: ordered tuple of `(id, cap, check)` with the ids and caps above;
  `check` is one of `none`, `one-thing`, `user-md`.
- `load_steps(path) -> dict[id, text]`: parse `## <id>` sections of
  FIRST-RUN.md. Raise if the ids don't match `STEPS` exactly, in order (a test
  runs it on the shipped `identity/FIRST-RUN.md`).
- `state(record_path, user_path)`: the active/finished rule above, including
  writing the "finished" record for an existing box.
- `step_line(state, texts) -> str | None`: None when finished. Otherwise
  "Onboarding step 2 of 6 (`stay-on-top`): …" + the step's text + the fixed
  rules; plus "Not done yet: …" when the last claim was rejected, and "Move on
  from X; they can come back to it" when the cap moved them.
- `after_turn(state, calls, commit_paths, user_path) -> state`: `calls` is the
  list of `(step, outcome)` from this turn's tool calls, in order. Increment
  the turn count; for each call naming the current step, apply the check and
  advance (several in one turn are fine); ignore a call naming any other step;
  then apply the cap. Persist atomically (write temp, rename).
- Tests: fresh install starts at `name`; existing box is finished and never
  gets a line; done advances; a `one-thing` claim without a matching commit
  path stays, with the "not done yet" line; `skipped` passes a failing check;
  two steps in one turn; a call for a non-current step is ignored; the cap
  advances on the Nth turn; finishing via `wrap-up`; the cap on `wrap-up`
  finishes with `capped`; a corrupt record fails to "finished", never to a
  restart; the shipped FIRST-RUN.md parses.

**Chunk 2: `server/onboarding_mcp.py`, the tool.** Copy the shape of
`server/weather_mcp.py`: stdlib only, no `server.*` imports, MCP stdio
framing by hand (`initialize`, `tools/list`, `tools/call`), run with `-I`.
- One tool, `step_done`, input schema `step` (string), `outcome` (enum
  `done`/`skipped`), `note` (string, short). Description says: call it when a
  step ends; the result is what to do next.
- Argv: `--steps <json>`, a list of `{"id", "text"}` from the current step to
  the end, built by the server each turn. It reads no file, keeps no state.
- Result: the text of the step after the one named, or "Onboarding is
  finished: close in a line." after the last; for an id not in the list,
  "That isn't the current step; the current one is `<id>`."
- Tests: the stdio exchange end to end through a subprocess, as
  `tests/` does for weather; each result case; malformed input never crashes.

**Chunk 3: wiring (not for the implementor).**
- `invoke.py`: during an attended turn with onboarding active, add
  `"onboarding"` beside `"weather"` in the `--mcp-config` JSON
  (`alwaysLoad: True`), never on any other turn. Extend `_tool_target` to
  record `step` and `outcome` for `mcp__onboarding__step_done` only, so the
  existing `ToolCall` log carries them (it never copies `tool_input` whole).
- `invoke._onboarding_for_turn` also builds the step line, put below the
  `[Context: …]` block (not in `turn_context.py`: invoke needs the same state
  for the tool's step list, and reads it once). It records the step it showed
  as `InvocationResult.onboarding_step`.
- `queue._note_onboarding`, after `commit_after_turn`: for a DONE chat turn
  whose prompt named a step, call `onboarding.after_turn` with the turn's
  `tool_calls` and `commit.paths`. A turn that showed no step never counts.
- `boundaries.py` vetted set + `deploy/marlow-settings.json` allow:
  `mcp__onboarding__step_done`, lockstep-tested like the weather tools. It
  takes no path and reaches nothing.
- `/v1/changes` reads the record through `onboarding.is_complete`, which never
  writes (it runs off the queue; only the queue's reads persist).
- Tests: the config includes the server only for an attended onboarding turn,
  never cron/system/compaction; the target extraction; the post-turn call; the
  settings lockstep.

**Chunk 4: identity (not for the implementor).** FIRST-RUN.md into step
sections, CLAUDE.md's first-run paragraph, the step line's fixed wording, the
`changes.json` entry.

**Chunk 5: activity label, both clients.** `web/src/lib/activity.ts` and the
iOS equivalent in `ios/Marlow/Marlow/Models/` map `mcp__onboarding__*` to
"Setting things up…", beside the weather lines, with a test on each side.
Small, but it is the one user-visible app change in phase 1, so it ships in
the same PR.

### Testing in a real run

Whether the model follows the step line and calls the tool shows only in a
real first run: a fresh install on the trial VM, which is also the
stranger-install follow-up. Never by nesting `claude -p` in a build session.
Write down what the run showed and what it didn't. Two more things only a run
answers:

- **The tool set changes mid-session.** The onboarding server is in
  `--mcp-config` only while a step is current, so the turn after wrap-up
  `--resume`s with one tool fewer. Check that resume accepts that, and what it
  costs the prompt cache (a changed tool list likely misses it once).
- **A box partway through the old script at deploy** has no record and still
  has the placeholder, so it restarts at `name`. Accepted: no box is
  mid-onboarding today, and the cap moves a repeated step on in a few turns.

### Phasing

Phase 1: chunks 1–5. Phase 2, both clients: fixed screens where no judgment is
needed (what an approval is, "tap here to see your Schedule", the mic), as
overlays that wait for a tap, the "video game tutorial" feel. Scope iOS and
`web/` together.

Parked until a run shows the need: pausing onboarding as a whole ("not now,
later"), and not counting real requests against the cap.

Related, already parked in `friends-alpha.md` → "Pairing papercuts": the
greeting wiping the big face, the device name, the QR opening the browser, the
Tailscale battery settings.
