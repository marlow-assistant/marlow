# Task — turn activity: live status and turn telemetry

**Status: shipped. Phase 0a (queue-wait telemetry) deployed 2026-08-19, gate read 2026-08-22 and passed. Phase 1 (server: stream-json, activity events) merged 2026-08-22. Phase 2 (iOS) is `ios-turn-activity.md`. Phase 3 (token streaming, found 2026-09-01) is `streaming-reply-text.md`.**

Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

Agreed with Mike 2026-08-19. The server half was first observed against a fixture taken from a box probe, not a live turn; the first honest read was deploying it and sending a message (see "as built", last bullets).

Captured from the wishlist on the box. It subsumes two entries that are one build: *live tool/work status instead of generic "thinking…"* (2026-08-12) and *turn telemetry / responsiveness* (2026-08-12, sharpened 2026-08-14). Building them separately means designing the same event stream twice. It also closed Known gap #4 in `marlow-v2-START.md` (iOS perceived latency).

## Why this is worth doing

Mike's complaint is 30s+ turns showing a static "thinking…" the whole way. The turns are probably not going to get much faster. **The fix for *perceived* snappiness is showing the work, not finishing it sooner.** His own hunch, and it matches what Claude Code's TUI does.

Corroboration from `model-and-effort-config.md`: the chat-shaped probe prompt ("anything I should know today?") spent 427 thinking tokens but **27 seconds**, because it reached for calendar and mail. On Mike's real traffic latency looked dominated by tool round-trips, with effort as noise next to it. (The 2026-08-22 probe below found local tool legs at 0.17s, so this is not established; network MCP calls and local ones differ.)

Telemetry is the same data with a different consumer: "why did that turn take 30 seconds" is answerable from timestamped stage events and is not answerable from anything the server records without them.

## The mechanism — confirmed, not assumed

`claude -p --output-format stream-json --verbose` emits line-delimited JSON as the agent works, ending with a `{"type":"result",…}` envelope carrying the same payload `--output-format json` returns.

**Re-probed on the box 2026-08-22, CLI 2.1.235, two runs** (over ssh in a clean shell, the only honest way to observe this; a nested run inside a Claude Code session inherits the harness env). The 2026-08-14 summary was right about the shape and **wrong in two places**.

**It genuinely streams.** Lines arrive spread across the turn, not flushed at the end. This was the one result that could have killed the design.

| | run 1 | run 2 |
|---|---|---|
| first line | 2.20s | 1.74s |
| `system`/init | 2.80s | 2.63s |
| `assistant` w/ `tool_use` | 5.01s | 4.27s |
| `user` w/ `tool_result` | 5.19s | 4.44s |
| `assistant` w/ `text` | 6.79s | 5.40s |
| `result` | 6.84s | 5.46s |
| process exit | 7.29s | 5.85s |

**Correction 1 — no text deltas *by default*.** The old summary claimed "assistant text deltas." Without an extra flag the reply arrives as one complete `assistant` message with one `text` block, 60ms before the result envelope. *(Superseded in part, 2026-09-01: the sentence that used to end this paragraph, "not available from this interface and must not be promised", was wrong, and wrong in the expensive direction. It was read as a property of the CLI when it was a property of the probe. `--include-partial-messages` adds `stream_event` lines carrying `content_block_delta`; see below.)*

**Correction 2 — `rate_limit_event` exists** and is the first line on both runs. This confirms a parser rule already written on principle: **skip unknown top-level types, never fail on them.** Observed types: `rate_limit_event`, `system`, `assistant`, `user`, `result`.

**New finding — the first ~2.7s emits nothing at all.** About 1.7–2.2s before the first line, 2.6–2.8s before `system`/init. That is CLI process startup, and it is invisible to `duration_ms` (4084ms on run 1, against a 7.29s wall). The UI shows a contentless spinner for the first stretch of every turn no matter what this feature does. On the measured 10.8s median attended turn that was ~25% of felt latency.

**Tool round-trips were 0.17s both runs** (local `Bash`). That is not what the "dominated by tool round-trips" claim looks like; here the model legs dominate. Both can be true (network MCP calls vs local ones). Do not treat the domination claim as established.

The envelope carries more that we do not use: `ttft_ms` / `ttft_stream_ms`, `duration_api_ms`, `time_to_request_ms`, `num_turns`, `stop_reason`, `terminal_reason`, `modelUsage` (which showed **both** `claude-sonnet-5` and `claude-haiku-4-5` in one turn).

**Negative result worth keeping:** `duration_ms - duration_api_ms` does **not** yield tool time. Run 1 gave `4084 - 4988 = -904ms`; `duration_api_ms` is not a subset of `duration_ms`. The cheap "get the split without streaming" shortcut does not exist.

**A denied `Bash` call returned `is_error: false`, `subtype: "success"`, with `permission_denials` length 1**, so CLAUDE.md's rule was observed live again. The denial record contains a `tool_input` field: it must never be logged.

### `thinking_tokens` — found, and the old column was dead

The real key is `usage.output_tokens_details.thinking_tokens`, not `usage.thinking_tokens`. `telemetry.py` read the latter and recorded `None` on 200 of 200 rows, attended and cron alike. Phase 0a had added it on the claim that it was "already present in the CLI's `usage` block"; it was not. Available in the plain `json` format too, so fixable independently of streaming. (Observed value 0 on both probe runs, a real 0: trivial prompt.)

*(The second doc claim in two days falsified by first contact with real output; the first was in `idle-compaction.md`. Both were "the CLI reports X" assumptions carried from a probe. Verify counter fields against a real row before building a read on them.)*

### `thinking` blocks have never been observed

The `ActivityEvent` contract lists `"thinking"` as a `kind`. Three probe runs produced zero thinking blocks, including one whose prompt said "think about X, *then* use a tool." Observed block types are only `tool_use`, `tool_result`, `text`. Either it is suppressed in headless print mode or it needs a flag we do not set. Keep the kind in the type, but do not wire UI to it and do not write a test that pretends to observe one: an unreachable branch that looks tested is worse than an obviously missing feature.

---

## Streaming IS available — probed 2026-09-01. Phase 3.

Mike, unprompted, a week after Phase 2 shipped:

> I've increasingly been reaching for the normal Claude or other AI assistant apps on my
> phone, because the responses are snappier … I'd really rather use Marlow for
> everything.

He guessed the cause was streamed responses. He was right, and the August probe had wrongly closed that door: it never passed `--include-partial-messages`, so it measured the default and concluded the interface could not do it.

**Re-probed on the box over ssh, CLI 2.1.251, `--model sonnet`, two runs.** With `--output-format stream-json --verbose --include-partial-messages`, a three-paragraph prose answer:

| | today (no flag) | with the flag |
|---|---|---|
| first text visible | 13.14s (the whole reply, at the end) | **3.22s** |
| deltas | 1 `assistant` message | 19 `content_block_delta` |
| chars / delta | — | 111 mean |
| streaming window | — | 9.87s, ~215 chars/s |

**Time to first word drops ~4x.** It is not token-by-token (deltas are clause-to-sentence sized), but what it replaces is nothing at all for thirteen seconds, and 111 chars every half second reads as live text.

The cost to build was small because `invoke.py` already ran attended turns on stream-json and mapped lines to `ActivityEvent`: one more flag, a delta branch in the mapper, and the client rendering partial text into the bubble it already had. *(Superseded 2026-09-28: every logged turn, cron included, now runs plain stream-json so the activity log sees its tools, `ux-lane.md` §2a. Only `--include-partial-messages` stays attended-only.)* The design is in `streaming-reply-text.md`.

**What this does to the decoupling question.** It largely removes the *product* argument for leaving the CLI. The cost argument already said no (`usage-telemetry.md` third read: $42–60/mo at API rates against a $20 Pro subscription). Both axes now point the same way, which is worth stating because for about an hour on 2026-09-01 they did not.

### The remaining floor: CLI startup

Still real, still unfixed, and now the dominant wait before first text. Across three runs on two CLI versions: **first line at 0.7–2.4s**, `system`/init shortly after, emitting nothing, invisible to `duration_ms`. Variable (a warm run hit 0.70s, a cold one 2.40s), so the earlier flat "~2.7s" is the pessimistic end.

With streaming shipped this is ~60–70% of time-to-first-word rather than ~25% of total turn latency. **Nobody has tried to cut it.** The obvious shape is a warm or speculatively-started process (the queue serializes invocations, so there is at most one to keep warm), but that trades against `invoke.py`'s scrubbed-env guarantee and the per-turn identity injection, neither of which survives a reused process without thought. Worth its own task; do not bolt it onto the streaming work. Findings and Mike's words, not a scoped build.

## Phase 0 — the telemetry read

Rationale: nearly free, and it says whether latency is where we think before any UI is designed around a guess. Phase 0a (no streaming): `telemetry.py` records `thinking_tokens` (see above, it was dead) and **queue wait**, the gap between `Turn.created_at` and the moment `_drain` invoked it. It can measure only queue wait; the tool-vs-model split needs stream-json parsing. So the gate was narrow: confirm queue wait is near-zero after a day or so, then go to Phase 1, else re-scope. Per-tool counts/durations (Phase 0b) were deferred; see the scope decision.

### Phase 0a read — 2026-08-22. **Gate PASSED.**

19 attended (`source == "ios"`) turns carry `queue_wait_seconds`, 2026-08-19 13:02 to 2026-08-22 17:18.

| | |
|---|---|
| queue wait — median / p90 / **max** | 0.001s / 0.001s / **0.001s** |
| turns over 0.1s | **0 of 19** |
| turn duration — median / p75 / p90 / max | 10.8s / 15.1s / 20.6s / 75.8s |
| turns over 10s | 11 of 19 |

Queue wait is 1ms on every turn with no spread: the single serialized queue with one user, as predicted. The felt problem was intact and unexplained (11 of 19 over 10s, queue is 0.01% of the median), so the time is in the invocation itself. That is the Phase 1 argument, backed by numbers rather than the effort-probe anecdote.

---

## Scope decision — Mike, 2026-08-22. **Reassurance, not accuracy.**

Shown the probe numbers (startup 2.65s, model 3.34s, tool 0.16s), Mike's call was explicit: *"it doesn't have to be perfectly accurate, just give some feedback that something's happening… I'm not looking for totally honest/accurate stuff, just better than no feedback for a long time."*

Target experience, in his words: "Thinking..." (startup) / "Using a tool..." (better: "Reading a file...") / "Thinking..." (tool done, model composing) / the reply.

Consequences:

- **Phase 0b (per-tool timings) is not a prerequisite and was deferred.** The feature is reassurance; do not gate the UI on measurement.
- **A label may be approximate.** It may lag, round or generalise. It must never be *wrong about kind* (do not say "Searching the web" for a file read) and must never leak tool input.
- **Minimum dwell time is required.** The local tool leg is 0.16s; without a floor the label strobes on every `Read`/`Bash`. Once shown, a status holds ~600–800ms and later events queue rather than overwrite. Invisible in any test without realistic timings.
- **Set expectations honestly.** On a local-tool turn the user still sees "Thinking…" for ~4.8 of 6.2s and the tool label is a blip. The payoff is on the 10s+ turns that reach network MCP servers (Gmail, Calendar), which is the complaint that started this. Do not evaluate on a fast local turn.

**Unresolved, deliberately not built:** an "unlabelled stretch is dragging" state ("Still working…" after ~2s of nothing). Pure reassurance, no new data. Not built without Mike asking.

**Render from structured data, never from model narration** (settled 2026-08-14, Mike's design note). Do not prompt Marlow to narrate its own actions ("Let me check three things…"): that reintroduces the filler `identity/CLAUDE.md` cuts, and costs tokens and latency to produce text we then display. Tool-event labels need no system-prompt change.

---

## Phase 1 — server: stream, capture, expose

### Phase 1 — as built, 2026-08-22

All four pieces landed together, because the read loop alone is unreachable code: nothing calls `on_activity` until the queue does. Attended turns pass `--output-format stream-json --verbose`; the final `result` line is parsed into the same `InvocationResult`. Cron stayed on `--output-format json` at the time (see the 2026-09-28 note above). What is worth knowing:

- **The attended gate is `active_turn.ATTENDED_SOURCES`, not `_effort_for_source`.** The plan said to reuse the effort split; that would have meant `source != "cron"`, which treats an unclassified future source as attended. `active_turn` fails the other way (unknown means unattended), the right direction when the consequence is changing the CLI's output format. Streaming also requires a caller that passed a callback, so every existing caller is byte-identical.
- **The read loop reassembles lines from chunks by hand rather than `StreamReader.readline()`.** readline raises past its 64KiB limit and the `result` line carries the whole reply, so the obvious version loses exactly the long answers most worth waiting for. A test with a 200KB result fails on the readline version.
- **stderr is read concurrently with stdout**, via `asyncio.gather`. Draining stdout to EOF first deadlocks on a CLI that fills the stderr pipe, which `communicate()` used to hide. The test hung for the full 20s timeout when the concurrency was mutated away.
- **`tests/conftest.py`'s fake CLI now honours `--output-format`.** It ignored the flag, so once attended queue turns streamed it was answering plain JSON to a streaming invocation, a CLI that does not exist. Now the whole queue/app suite exercises the streaming path.
- **`Turn.activity` is cleared in `_drain`'s `finally`**, so DONE and both ERROR paths clear alike. A stale "Using a tool…" under an error message forever is worse than the silence this replaces. (Same rule `idle_compaction.py` learned: state that only updates on success leaves a confident wrong picture.)
- **Review fixes, same day** (`/code-review medium`, all in `invoke.py`): the two reads are explicit tasks with an explicit kill-and-reap on the failure path (`asyncio.gather` propagates the first exception but does NOT cancel its siblings, so a read loop that raised left `claude -p` running while the queue started the next turn, the concurrent invocation the single queue exists to prevent); the "no result line" error quotes the **tail** of the stream, since the head is only ever the rate_limit_event and the init line.
- **Security review, same day: the result envelope is merged, not picked.** `_final_envelope` takes the LAST `result` line for content but **ORs `permission_denials` and `is_error` across every envelope in the stream**, so a later line cannot clear a denial the CLI already reported. The OR is also just the right semantics without any attacker: if a CLI ever reports per-sub-turn, "a denial anywhere in this invocation counts" is the answer you want. Rejected: requiring the envelope to be the last line, or refusing a stream with two. That pins us to a format nobody has promised; a CLI that grew a trailing telemetry line would break every attended turn while cron kept working, a self-inflicted outage on a version bump with CI off. `result` itself is deliberately not defended: a prompt-injected model already writes the reply, so forging it is no new capability.
- **The single slot was wrong, and production said so within an hour** (2026-08-22, Mike). Deployed, three streamed turns, one of which read a file, and the design could not show it. The client polls every 2s (`PollingPolicy.fastInterval`) while a local `Read` leg measures ~0.16s, so a latest-wins slot is sampled roughly 8% of the time. **invoke.py already emitted every event; the loss was in queue.py, three lines before it reached the client.** `Turn.activity` became `Turn.activities`, a cumulative capped list (`MAX_TURN_ACTIVITIES = 64`, oldest dropped; the original objection to a list was unbounded growth in `_turns`, which the cap answers); the payload key is `activities`, an array. The client replays it as a queue with a minimum dwell, which reinstates the dwell requirement `ios-turn-activity.md` had told its implementor to drop, now as pacing rather than anti-strobe. Changed while the wire shape was still free: nothing consumed `activity`, so this was server-only. A day later it would have been a coordinated two-sided change.
- Every stream-json assertion was against fixtures from the box probe, not a live turn, until the first deployed read above.

### 1.2 The event contract — what crosses the wire

One small type: `ActivityEvent(kind, tool, at)` with `kind` in `tool_start | tool_end | thinking | text`, `tool` the name only, `at` a timestamp for ordering.

**PRIVACY — the decision that matters most.** `tool_use.input` carries file paths, file contents, search queries and email subjects. `invoke.py` already logs denials by name only for the same reason, and `telemetry.py` holds the same no-content-in-logs line. **Phase 1 sends the tool NAME and nothing else. No `tool_input`, not one field.** A name suffices for every label the UI needs ("Searching the web…", "Reading a file…", "Checking your calendar…"); a richer payload buys nothing and leaks by default if you forward the whole event. If a later phase wants detail ("Reading `travel.md`"), add a **named allowlist of specific fields per tool**, decide then whether it may be persisted, and treat it as its own review. (That allowlist now exists as the per-tool `target` in `ux-lane.md` §2a; `ActivityEvent` itself stays name-only.) Individual events must not reach `telemetry.py` rows, the server log, or anything that outlives the turn; aggregates (counts, durations per tool name) are fine.

### 1.3 `server/queue.py` — hold the current activity

The turn holds the activity (single slot at first, then the capped list above), a callback in `_drain` feeds it, and it is cleared on every terminal outcome. Phase 0b (per-tool aggregates handed to telemetry at terminal time) stayed deferred.

### 1.4 `server/app.py` — expose it

`_serialize_turn` carries the activity on both `GET /v1/turn/{id}` and `GET /v1/pending`. **No new transport, no new endpoint**: the client already polls every 2s in the fast phase, and a status line that updates faster than a person reads it is not better. This does not conflict with `chat-poll-slow-phase.md`'s "do not add engineering to this path": that ruling is about slow-phase retry machinery, not the payload the fast phase already fetches. `PollingPolicy` is untouched.

## Phase 2 — iOS

Split out to `ios-turn-activity.md` (2026-08-22), which is authoritative for the client half. Labels are mapped client-side from the tool name; unknown names, including MCP ones, degrade to a default and never render a raw `mcp__…` identifier.

## Scope boundary, and what it left open

Phase 1+2 shipped a single status line that vanishes when the turn completes. Activity is not kept in the transcript and one turn still produces one reply bubble. That default is the part most likely to want revisiting, and Mike's observation (2026-08-16) is why:

> tool use sprinkled in between assistant text will create more natural breaks
> for the assistant text to render

If text streaming follows, the unit is a paragraph between tool calls, not a token. That lands on a real client change: one input turn producing several speech bubbles, which touches the message model (parent turn id and ordinal), retry, dedupe, notification content and the "did the reply arrive" logic. Two questions were left for then: whether activity is discarded or kept inline as collapsed chrome (multi-bubble pushes toward kept, where the privacy decision starts to bite), and whether assistant text streams at all.

## Do not

- Do not let this become the on-ramp to a general event bus. One field, one consumer, one screen.
- Do not forward `tool_input` in any form.
- Do not add a WebSocket, SSE or a new endpoint.
- Do not touch `PollingPolicy`.
- Cron stays off streaming-only paths. *(Lifted 2026-09-28 for the persisted activity log, `ux-lane.md` §2a.)*

**Red/green edit diffs are NOT in scope** (corrected 2026-08-16, Mike's call). The wishlist entry "friendlier display of tool use" reads like a streaming concern, but the raw monospaced dump he was looking at is `PermissionSheet`'s `inputSummary`, the approval prompt before the tool runs. It has its own home in `ios-permission-round-trip-ui.md` §"Future — this is the edit Marlow wants to make": `Edit`'s `tool_input` already carries `old_string`/`new_string`, so no streaming is required.

## Verifying on the box

Streaming behaviour cannot be trusted from a nested `claude -p` inside a Claude Code session; it inherits the harness env and lies. Use a clean-shell script following `scripts/probe-*.sh`, then a slow turn on the phone showing changing labels. The privacy invariant, pinned by a test, is that `_serialize_turn` never emits tool input.
