# Backlog

**Status:** the single list of open work, opened 2026-11-12. One line per
item, with a pointer. It is an index, not a status board: an item's task doc
owns its status, and when the two disagree the task doc wins (fix the line
here). Ideas with no task doc yet keep a short sketch at the bottom.

What to build next, and why, is `backlog-analysis.md`. Where ideas come
from: Mike's wishlist on the box
(`~/marlow-identity/memory/marlow-features.md`), the only place Marlow can
capture one mid-conversation, and builder sessions.

**Keeping it current.** A new task doc adds its line here in the same
commit. When something ships, or is closed or parked for good, its line
goes; git keeps it. When a sketch below gets a task doc, it becomes a
one-line entry and its box wishlist entry gets a `→` pointer.

Tags: *not started*, *specced* (ready for an implementor), *in progress*,
*parked* (needs a trigger or Mike's call), *watching* (nothing to build yet).

## Features

- **Model picker** (`model-picker.md`): Sonnet or Opus, box-wide, chosen in
  Settings; "Think time" relabels effort. *in progress*
- **Friends alpha** (`friends-alpha.md`): the polish pass, then invite the
  first one or two friends. *in progress*
- **Share and files** (`share-and-files.md`): share sheet in, files out,
  four phases. *specced*
- **Image viewer** (`image-viewer.md`): full-screen image view. *specced*
- **Message retention setting** (`message-retention-setting.md`). *specced*
- **Drive in the approval card** (`tool-approval-ux.md` pieces 2–3): show
  the document's title, then standing per-doc read approval. *not started*;
  Drive gives `claude -p` no tools yet (`onboarding-v2.md`).
- **Seeded morning brief** (`default-infrastructure.md` step 4). *not started*
- **To-dos backed by Trello** (`todos-and-reminders.md` part 3). Direction
  only.
- **Ideas seen-state sync** across clients (`ideas-seen-sync.md`). *not started*
- **Push the ideas spotlight** (`ideas-panel.md`): none has been set on any
  night yet. *watching*
- **Connectors without the startup lag** (`connector-startup-latency.md`),
  so slow ones like Trello can come back. *not started*
- **Which connectors Marlow sees, and what it may do with them**
  (`connector-policy.md`). Feature review *not started*.
- **Route permission requests to the right device**
  (`multi-device-permission-routing.md`). Placeholder.
- **Side chats** (`side-chats.md`). *parked*
- **README demo clip** (`readme-demo-clip.md`). *parked*, a maybe.
- **Hosted Marlow** (`hosted-marlow.md`). Far future.

## Polish and bugs

- **Walk a friend's first day** on the trial VM (`pre-invite-polish.md`
  item 1). *not started*
- **Unsent draft survives a kill; long replies read from the top; What's New
  fade** (`ios-chat-ux-gaps.md` #20, #22, #21), both clients. *not started*
- **Android back closes the top layer** (`android-back-gesture.md`), web
  only. *not started*
- **Re-pairing to a new box keeps the old conversation**
  (`repair-against-a-new-box.md`). *not started*
- **The rest of the iOS gaps** (`ios-chat-ux-gaps.md`).
- **Two definitions of "retry this"** in ChatStore
  (`ios-unify-transient-retry-classification.md`). *not started*, a design
  call.
- **GPL pronunciation data in every iOS build** (`dependency-licenses.md`):
  blocks TestFlight or any handed-out build. *not started*
- **Transcript persistence and durability** (`ios-transcript-scaling.md`, the
  halves after rendering). *parked*
- **Snappiness** (`reply-length-and-perceived-speed.md`): tool turns still
  ~12 s median to first word. *watching*

## Waiting on a real-device check

Built and merged; only a phone (or a box that turns the feature on) can close
these. Each task doc lists exactly what is unseen.

- Read aloud on either phone (`read-aloud.md`).
- Android: voice mode on the Pixel (`voice-mode-android.md`), push through
  Doze and the soft keyboard (`portable-web-client.md`), the location prompt
  (`message-location.md`), faces (`marlow-faces.md`), What's New
  (`whats-new.md`), the Usage panel being found (`usage-visibility.md`).
- The Usage panel's ≥90% yellow row, either client (`usage-visibility.md`).
- A push to the second instance's phone (`marlow-for-taylor.md`).
- Funnel on real hardware (`funnel-ingress.md`), when a box turns it on.
- The next real first run (`onboarding-v2.md`).

## Infrastructure, security, durability

- **Connector drops at turn start** (`connector-drops.md`). *watching*
- **Egress enforcement and unattended WebFetch** (`egress-and-webfetch.md`).
  *parked* on Mike's NemoClaw re-check.
- **Alert on a failed timer run** (`failure-alert-channel.md`). *parked*
- **The `deploy/README.md` §7 watchdog** isn't installed. No task doc.
- **`--bare` becoming the `-p` default** (`bare-default-risk.md`). *watching*
- **Confine promoted cron scripts** (`cron-script-confinement.md`). *not started*
- **Turn retention numbers** (`turn-retention.md`): undecided, Mike's call.
- **Probe suite / doctor mode** (`probe-suite-doctor-mode.md`). *parked*
  until a second occasion to run it.
- **Usage telemetry phase 2**, a standing reader (`usage-telemetry.md`).
  *parked*
- **Nightly consolidation piece 3** (`nightly-consolidation.md`; read
  `memory-file-shape.md` first). *parked*
- **Same-turn store validation** (`same-turn-store-validation.md`). *parked*
- **Chat poll slow phase** (`chat-poll-slow-phase.md`): removal, not
  addition. *parked*
- **Parked by decision, each with its trigger:** `run-claude-cancellation.md`,
  `deferred-cron-review-findings.md`, `web-client-review-leftovers.md`,
  `install-review-leftovers.md`, `auth-notify-state-across-restarts.md`,
  `resolve-paths-once.md` (the expensive half), `direct-port-ingress.md`,
  `phone-access-without-vpn.md`.

## Ideas without a task doc

Sketches: the symptom, what we already know, and the first step. None is a
spec, and the order means nothing.

### To-do ids in replies (raised 2026-11-10)

Marlow referred to to-dos by id ("t13"), which the app never shows. Fixed by
a behaviour note on Mike's box only; `identity/CLAUDE.md` ("To-dos") does not
say it, so every other box still does it. One line in the template: name a
to-do by a short natural phrase; ids stay in tool calls. Needs a
`changes.json` entry.

### Notification flood after a computer wakes (raised 2026-11-10)

After Mike's PC sleeps, waking it brings minutes of back-to-back Marlow
toasts, one per message already read on the phone. Likely the push service
delivering everything it queued while the machine slept, with
`web/src/sw.ts` showing each because no window is focused. Direction:
the service worker drops a push older than a few minutes, or one for a
message another device has already read, and collapses a backlog into one
notification (`tag` replacement). Read `web-push-on-screen.md` and
`web-double-notification.md` first; the latter's cause turned out not to be
the web client.

### Garmin connection (raised 2026-10-22)

Pull runs, sleep and other health data straight from Garmin Connect instead
of Mike sending screenshots for Marlow to transcribe. Garmin over Strava
because it carries sleep and health data. The hold until open-source lifted
2026-10-28. Garmin's official API is partner-only, so the options are
unofficial community MCP servers that log in with the user's own Garmin
credentials; at least one is read-only and caches tokens to avoid lockouts.
Unvetted: maintenance status, what it does with the credentials, and how it
fits `connector-policy.md`, `egress-and-webfetch.md` and the
`AGENT_CONFIG_PATHS` rule (a local MCP entry is installed by the operator,
never by Marlow). First step: read the candidate repos, then a probe like
`scripts/probe-weather-mcp.sh`. Health data is the most sensitive store
Marlow would hold; the credential has to live in the env-secrets path.

### Friendlier display of tool use (raised 2026-08-12)

Render file edits as red/green diffs in the transparency sheet rather than
raw tool input. The live status line shows that something is happening; this
is about showing what it did. Unblocked since tool activity and reply text
both stream. Both clients. Keep it out of the message flow
(diagnostics belong in the transparency sheet).

### Standing "act while I'm away" tasks (raised 2026-09-28)

Narrow single-purpose watchers ("tell me if this listing changes") that keep
going after the conversation ends. Schedules already run fixed prompts the
user approved; this would act on Marlow's own judgment, so it needs a design
pass on guardrails first: what a watcher may do besides notify, how it
expires, and the unattended-WebFetch exposure in `egress-and-webfetch.md`.
A notify-only watcher built on the schedules store is the small version.

### Protocol overhead audit (raised 2026-08-21)

Mike's sense that a Marlow send/receive is "more work" than other
assistants' apps. Measure before changing anything: request and response
sizes and round trips for one turn, from the client. Related:
`reply-length-and-perceived-speed.md` (2026-11-04 read: tool turns median
12.4 s, p90 ~31 s, no-tool turns ~5 s, so lookups dominate, not transport)
and `chat-poll-slow-phase.md` (direction is removal, not addition).

### Anonymised usage report from other users (raised 2026-10-15)

A prompt a friend runs on their own Marlow to write an anonymised report of
how they use it, which they read before sending to Mike. The draft prompt is
on the box entry. Could ship as a skill or a Settings action once reviewed;
the report never leaves the box without the user sending it.

### Embeddable maps in replies (raised 2026-08-16)

An inline map card with labelled pins for "what's nearby" questions. Needs a
map component on both clients and a place data source. Mike's own caveat:
real but lower priority.

### Quota-aware memory maintenance ("dreaming", raised 2026-09-03)

A wider silent pass over all of `memory/`, timed to spend quota that would
otherwise reset unused. The `rate_limit_event` data answers "when does the
window reset". Weakened by `memory-file-shape.md`, which argues for a cheap
structural sweep, and by `consolidate` now ageing out past-dated entries.

### Google Doc to markdown on disk (raised 2026-08-18)

Fetch a Drive file straight to a markdown file without a base64 round-trip
through the model's context. Blocked upstream: the Drive connector gives
`claude -p` no tools (`onboarding-v2.md`; re-check with
`scripts/probe-drive-connector.sh` after a CLI update).

## Not on the backlog on purpose

- **A model-agnostic runtime** off `claude -p`: contradicts the first hard
  constraint in `CLAUDE.md`. Mike's to reopen, never an ordinary task.
