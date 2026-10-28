# Task — iOS: show what the turn is actually doing

**Status: DONE, merged 2026-08-23**, on top of the server half (merged 2026-08-22). Phase 2 of `turn-activity-streaming.md`. `activities` is live in `APIModels.swift` and `ChatStore.swift`. (This line read "awaiting review" until 2026-09-01, nine days after it merged.) The contract below is kept because it is the **wire format**, not a plan: read it before changing the payload.

Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

## What this is for, in Mike's words

> it doesn't have to be perfectly accurate, just give some feedback that
> something's happening… I'm not looking for totally honest/accurate stuff,
> just better than no feedback for a long time.

A running turn showed `"Thinking…"` and a spinner for the whole invocation, and 11 of 19 measured attended turns took **over 10 seconds** (median 10.8s, max 75.8s). The target: "Thinking…" (startup), "Reading a file…" (a tool is running), "Thinking…" (tool done, model composing), then the reply. **Reassurance, not accuracy.** A label that is a step behind or misses a transition is a success; a label that is *wrong for a long time*, or that outlives its turn, is the only real failure.

## The wire contract

`GET /v1/turn/{id}` gains one key, `activities`, an array of `{kind, tool, at}` (`kind` one of `tool_start`, `tool_end`, `text`, `thinking`).

- It is **cumulative** for the turn, oldest first, capped at the latest 64 events (oldest dropped on overflow). It is **`[]`** whenever there is nothing to show (before the first tool call, on every unattended turn, and **always once the turn is terminal**: the server empties it on DONE, ERROR and CANCELLED alike). Never null, never absent on a current server.
- **It is an array because the client samples, not streams.** A single "what is it doing right now" field was the original design and could not show a 0.16s file read to a 2s poll at all.
- `tool` is the tool **name only**, non-null only on `tool_start`. There is deliberately no tool input on the wire. `at` is a server-side epoch float, for debugging; prefer your own receipt time for client timing.

**Treat every field as optional and every value as unknown.** An unrecognised `kind` from a CLI upgrade falls back to the generic label, never crashes and never shows raw server text. An absent key decodes as `[]`, so an older server leaves today's behaviour untouched.

## What shapes the design: sampling, not streaming

The server streams; **the client polls every 2 seconds** (`PollingPolicy.fastInterval`). Measured on the box (CLI 2.1.235, three runs, a local `Bash` turn):

| leg | duration |
|---|---|
| CLI startup, nothing emitted at all | ~2.7s |
| model deciding | ~2.1s |
| **tool round-trip (local)** | **~0.16s** |
| model composing the reply | ~1.2s |

1. **A 0.16s tool leg is invisible to a latest-value poll**, but the cumulative array preserves it for the next poll. **Do not shorten the poll interval**: it was briefly 1s on the feature branch and went back to 2s on 2026-08-23. The array is what stops events being missed, so a shorter interval buys no extra events; it only doubles `GET /v1/turn` *and* `GET /v1/permissions` for every in-flight turn, and the phone wakeups with them. If labels feel laggy, the dwell is the knob, not the poll.
2. **The first ~2.7s of every turn emits nothing**, so `activities` is empty on the first poll; `"Thinking…"` is correct there.
3. **Judge the feature on 10s+ turns that call network MCP tools** (Gmail, Calendar), where a leg lasts seconds and a 2s poll lands inside it. A fast local turn will look broken when it is working as designed.

### Minimum dwell time — REQUIRED. (This reverses an earlier instruction.)

An earlier version of this doc said to drop the ~700ms minimum dwell, reasoning that a label cannot change faster than the 2s poll. That was correct for the single-slot design and is **wrong now**: with a cumulative array the client replays a queue, and the dwell is what makes each entry visible at all. Without it, three events arriving in one poll render as one instantaneous flicker ending on the last. So **show each label for at least ~700ms before advancing**; if more than ~3 events are unshown, **skip ahead** to the last few rather than draining a backlog minutes behind reality; **coalesce consecutive events that map to the same label** (`tool_end` then `text` both mean "Thinking…" and dwell once). The client tracks each message's previous server window and finds its overlap with the next poll, so cursoring survives the 64-event cap rolling forward (a full window sliding forward must still reveal the newly appended event). When the answer arrives, drop any remaining backlog rather than finishing the replay. **The label and playback state are cleared on every terminal transition**, `.done` and `.error` alike; that is the test that matters.

### The label mapping

Friendly per-tool phrasing with a generic fallback (Mike's pick): `Read` "Reading a file…", `Write`/`Edit` "Writing a file…", `Bash` "Running a command…", `Grep`/`Glob` "Searching…", `WebSearch`/`WebFetch` "Searching the web…", MCP calendar tools "Checking your calendar…", MCP mail tools "Checking your mail…", anything else "Using a tool…". MCP names arrive as `mcp__<server>__<tool>` with whatever server segment the connector calls itself, so **match on a substring (`calendar`, `gmail`/`mail`) case-insensitively** rather than hardcoding a full name a connector rename would silently break. Every `kind` other than `tool_start` renders `"Thinking…"` as one branch. **Never render `tool` itself:** MCP tool names are declared by the connector, so an unmapped one is arbitrary text of arbitrary length. It selects a label; it never becomes one.

## Not built

- **No typewriter / token-by-token effect** at the time: probed, and the default interface emitted no text deltas. *(Superseded: found 2026-09-01, shipped 2026-09-03 — `--include-partial-messages` makes streaming possible; see `streaming-reply-text.md`.)*
- **No stall or "still working" state**; Mike explicitly chose to leave it out.
- **No new polling, no second endpoint, no push.** `activities` rides the poll that exists.
- **Nothing keyed off `kind == "thinking"`.** No `thinking` block was ever observed across three probe runs, including one whose prompt asked the model to think before acting; treat it as unreachable and let it fall into the generic branch.

## Ordering and first contact

The server half was merged 2026-08-22 and the box redeployed from `main` on the morning of 2026-08-23, so a live turn already returned `activities`. **Nobody had seen it work end to end** when this client was built: the server's stream-json handling was tested only against fixtures from a box probe, never a live CLI driving a real turn. The first on-device run was the real test of both halves. (A later `xcodegen generate` reminder applies as always: a new test file not in the project is a false pass on the only gate.)
