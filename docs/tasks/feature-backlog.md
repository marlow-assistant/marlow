# Feature backlog — wishlist items with no task doc yet

**Status:** a holding pen, opened 2026-11-12. Mike's wishlist is captured on
the box (`~/marlow-identity/memory/marlow-features.md`, see
`marlow-v2-START.md`), because that is the only place Marlow can write
mid-conversation. Items there that already have a task doc point at it; the
ones below had none, so they were invisible from this repo. When one is
picked up, it gets its own task doc and leaves this file, and the box entry
gets a `→` pointer.

Each entry is a sketch: the symptom, what we already know, and the first
step. None is a spec. Ordered by the 2026-11-12 prioritisation (usefulness
first).

## 1. Opus as an option (raised 2026-11-11)

Let Marlow run on Opus 5.5 as well as Sonnet 5.5. The model today is one
per-box setting (`MARLOW_MODEL`, default `sonnet`); effort is per job.
`model-and-effort-config.md` ("Per-job effort, and why not Opus",
2026-10-18) already measured the cost: Opus for `consolidate` alone adds
~20% to a box's API-equivalent total, Opus for every job ~55%, and `ideas`
and `consolidate` must move together because they share a cache. It also
records the decision not to put a model toggle on the clients.

Open: who picks (a per-entry `"model"` key in `cron/schedules.json` that
Marlow sets itself, like `"effort"`, versus a chat-side switch, which pays a
cold prefix on every change); whether it falls back to Sonnet when the
`rate_limit_event` window runs low. The cheapest first step is per-entry
model on the research jobs nobody waits on, mirroring per-entry effort.

## 2. To-do ids in replies (raised 2026-11-10)

Marlow referred to to-dos by id ("t13"), which the app never shows. Fixed by
a behaviour note on Mike's box only; `identity/CLAUDE.md` ("To-dos") does not
say it, so every other box still does it. One line in the template: name a
to-do by a short natural phrase; ids stay in tool calls. Needs a
`changes.json` entry.

## 3. Notification flood after a computer wakes (raised 2026-11-10)

After Mike's PC sleeps, waking it brings minutes of back-to-back Marlow
toasts, one per message already read on the phone. Likely the push service
delivering everything it queued while the machine slept, with
`web/src/sw.ts` showing each because no window is focused. Direction:
the service worker drops a push older than a few minutes, or one for a
message another device has already read, and collapses a backlog into one
notification (`tag` replacement). Read `web-push-on-screen.md` and
`web-double-notification.md` first; the latter's cause turned out not to be
the web client.

## 4. Garmin connection (raised 2026-10-22)

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

## 5. Friendlier display of tool use (raised 2026-08-12)

Render file edits as red/green diffs in the transparency sheet rather than
raw tool input. The live status line shows that something is happening; this
is about showing what it did. Unblocked since tool activity and reply text
both stream. Both clients. Keep it out of the message flow
(diagnostics belong in the transparency sheet).

## 6. Standing "act while I'm away" tasks (raised 2026-09-28)

Narrow single-purpose watchers ("tell me if this listing changes") that keep
going after the conversation ends. Schedules already run fixed prompts the
user approved; this would act on Marlow's own judgment, so it needs a design
pass on guardrails first: what a watcher may do besides notify, how it
expires, and the unattended-WebFetch exposure in `egress-and-webfetch.md`.
A notify-only watcher built on the schedules store is the small version.

## 7. Protocol overhead audit (raised 2026-08-21)

Mike's sense that a Marlow send/receive is "more work" than other
assistants' apps. Measure before changing anything: request and response
sizes and round trips for one turn, from the client. Related:
`reply-length-and-perceived-speed.md` (2026-11-04 read: tool turns median
12.4 s, p90 ~31 s, no-tool turns ~5 s, so lookups dominate, not transport)
and `chat-poll-slow-phase.md` (direction is removal, not addition).

## 8. Anonymised usage report from other users (raised 2026-10-15)

A prompt a friend runs on their own Marlow to write an anonymised report of
how they use it, which they read before sending to Mike. The draft prompt is
on the box entry. Could ship as a skill or a Settings action once reviewed;
the report never leaves the box without the user sending it.

## 9. Embeddable maps in replies (raised 2026-08-16)

An inline map card with labelled pins for "what's nearby" questions. Needs a
map component on both clients and a place data source. Mike's own caveat:
real but lower priority.

## 10. Quota-aware memory maintenance ("dreaming", raised 2026-09-03)

A wider silent pass over all of `memory/`, timed to spend quota that would
otherwise reset unused. The `rate_limit_event` data answers "when does the
window reset". Weakened by `memory-file-shape.md`, which argues for a cheap
structural sweep, and by `consolidate` now ageing out past-dated entries.

## 11. Google Doc to markdown on disk (raised 2026-08-18)

Fetch a Drive file straight to a markdown file without a base64 round-trip
through the model's context. Blocked upstream: the Drive connector gives
`claude -p` no tools (`onboarding-v2.md`; re-check with
`scripts/probe-drive-connector.sh` after a CLI update).

## Not here on purpose

- **A model-agnostic runtime** off `claude -p`: contradicts the first hard
  constraint in `CLAUDE.md`. Mike's to reopen, never an ordinary task.
