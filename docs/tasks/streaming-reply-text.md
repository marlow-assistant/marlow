# Task — stream the reply text, not just the status line

**Status: SHIPPED 2026-09-03.** Merged to `main` and deployed. Both halves are live: the flag, the delta parser, `Turn.partial_text` + its tool marks, the serializer, and the iOS decode/smoothing/render, plus two things a phone in hand revealed the same evening (the 500ms burst for the first 6s, and dropping the status line while streamed text is on screen). Streaming works, the cadence feels right, and airplane-mode mid-reply recovers. Follow-ups shipped 2026-09-04 (reply delivered whole) and 2026-09-08 (no replay of an already-written reply).

Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

Phase 3 of `turn-activity-streaming.md`. Designed 2026-09-01 from measurements on the box the same evening, not reasoned from the docs: the doc it descends from was wrong about the central fact for ten days, so nothing here is inherited on trust. **Why it was top of the list:** Mike was reaching for the stock Claude and other AI assistants' apps for general queries because they felt snappier, and would rather use Marlow for everything.

**What the build changed in the design.** The pacing rule "size each tick from the current backlog" is wrong as stated: recomputing from a shrinking backlog decays geometrically and never finishes, so the tail of every reply arrives late, the latency tax the rule exists to prevent. The stride is computed **once per arriving slab** (`ceil(backlog / ticksPerPoll)`) and held until the next (`PartialTextPlaybackState.stride`).

**Cron parity confirmed live 2026-09-03.** The 04:30 morning job logged `streaming=False`; the four phone turns after logged `streaming=True`; the unattended resume at 04:56, same session id as the phone turns, also logged `streaming=False`. The gate keys on the *source*, not on whether a session was ever attended: `active_turn.ATTENDED_SOURCES` is the whole control, and an unattended path riding a phone session stays off the flag. *(Superseded 2026-09-28: every logged turn now runs plain stream-json for the activity log, `ux-lane.md` §2a; only `--include-partial-messages` stays attended-only.)*

## Follow-up 2026-09-08 — the typewriter replayed a reply already written

**Found in use, 2026-09-05:** Mike opened the app on an unread message and watched a turn that had finished long before type itself out. `PartialTextPlaybackState` had no notion of *when it started watching*: it holds `revealed = ""` and a `target` that is the server's cumulative reply. On relaunch, `reconcilePendingMessages()` correctly drops the persisted `streamedText` (otherwise the first tick rewinds the bubble from a sentence to a few characters) and `startPoll` re-ingests the whole cumulative string into a state that has revealed nothing, so every character gets typed. The 2,000-character `partialTextJumpThreshold` is sized for a phone coming back from a tunnel with an essay buffered and doesn't cover an ordinary reply.

**The rule: "type only what arrived while we were watching."** A state built for a turn already underway reveals its first slab whole and paces everything after it. The discriminator is **"did this state join late"** (carried from `reconcilePendingMessages()` through `ChatStore.resumedPlaceholders`), deliberately not **"is this the first ingest"**, which is equally true of a live turn's first text and would flatten the effect this task exists for. A test pins exactly that (`aTurnWatchedFromTheStartIsStillTypedOut`): it is the one-character change that makes the feature silently do nothing while every other test passes.

## The measured ground

`claude -p --output-format stream-json --verbose --include-partial-messages`, CLI 2.1.251, `--model sonnet`, on the box over ssh:

| | today | with the flag |
|---|---|---|
| first text visible | 13.14s (whole reply, at the end) | **3.22s** |
| deltas | 1 `assistant` message | 19 `content_block_delta` |
| chars per delta | — | 111 mean (1 char on a short reply; it varies) |
| streaming window | — | 9.87s at ~215 chars/s |

Line shape: `{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"h"}},"session_id":…,"parent_tool_use_id":null,"uuid":…}`. Three properties decide the design:

1. **There is no `message` key**, so `parse_activity_line` returns `None` for every `stream_event` line. **Adding the flag is inert** until a branch is added for it; ship the flag and the parser separately.
2. **`event.index` identifies the content block**, and `content_block_start` carries `content_block.type` (`text` or `tool_use`). That pair is how a delta is attributed to text-versus-tool without guessing.
3. **The last line is still `result`**, byte-identical to `--output-format json`. Load-bearing and exactly what a CLI upgrade breaks silently, so pinned by a test.

## Does polling survive this? Yes — and polling is the right answer

Mike's instinct that the architecture is "naive REST polling" is accurate as a description and wrong as a criticism. `POST /v1/message` returns a `turn_id`; the client polls `GET /v1/turn/{id}` every 2s for ~5 minutes, then every 30s for another hour. **Do not replace it with SSE or a WebSocket:**

- **A long-lived stream on spotty mobile is a stream plus a resume protocol.** The connection drops in a tunnel; you must detect the half-open socket, reconnect and resume from a cursor, so the server must retain a replayable log of what it sent. That log *is* the snapshot mechanism. You would build snapshot polling anyway, as the recovery path, with the socket as extra surface.
- **iOS suspends the app.** A backgrounded URLSession stream is killed; a poll loop resumes with one request and no state.
- **A missed poll is self-healing.** The next one returns more text: no gap semantics, no duplicate semantics, no cursor to get wrong.
- **`ios-poor-network-timeouts.md` already bounded the send path** and the poll loop has fast/slow phases. A socket would inherit none of that hardening.

**So: keep polling, and make the payload cumulative.** The server accumulates deltas into a monotonically growing string; each poll returns the whole thing. The client renders whatever it last received: `if server_len > local_len: render(server_text)`. This buys what the `activities` design didn't: that capped sliding window had to invent longest-overlap matching in `ActivityPlaybackState.ingest`. Do not repeat the window pattern for text. Payload cost is a non-issue: a long reply is a few KB, ~12KB per turn re-sent across eight polls, against images already measured at ~300KB. If it ever matters, `?since=<n>` is a pure optimisation, since the model is "the server has a string and you can ask for a suffix of it."

## The granularity problem, and the right fix

2s polls against ~215 chars/s means text lands in ~430-char slabs: a paragraph at a time. A large improvement on thirteen seconds of nothing, but not what makes the other apps feel alive. **Fix it on the client, not with a faster poll:** buffer received text and render it out at a measured rate, the same idea as `ActivityPlaybackState`'s 700ms `activityDwell`. Two rules keep smoothing from becoming a latency tax: **cap the buffer** (if backlog exceeds about one poll interval of playback, speed up or jump), and **snap to the end on completion**. Resist shortening `fastInterval` until this has been felt on a real phone; more polls cost battery and smoothing may make the cadence invisible.

## Interleaving — measured, not guessed

Mike flagged this as the hard case. It is real, and a minority. Across **113 turns** reconstructed from the box's CLI transcripts (split on real user messages, not transcript files: a transcript is a whole resumed session and per-file counting overstates by ~5x):

| Shape | Turns | |
|---|---|---|
| tool(s) first, then text | 54 | 47.8% |
| **text only, no tools** | 46 | **40.7%** |
| interleaved (text, tool, text) | 12 | 10.6% |
| tools only, no text | 1 | 0.9% |

**89% of turns contain exactly one text block** (mean 1.14, max 5). The 41% with no tools is Mike's stated use case ("general queries", the turns he takes to other apps) and streams first token to last with no interleaving question. The 48% tool-then-text case needs nothing new: the status line covers the tool phase and the answer streams. **A single accumulating string is correct for 89% of turns.** The 11% needs the text to know where the tool went; concatenating blindly produces "I'll check your calendar.You have three meetings", two thoughts fused.

**Shape: one string, plus a marker list.** Accumulate into a single `partial_text` and record the character offset of each tool call; the client renders the string and injects the tool chip at those offsets. Reach for a full ordered segment list only if markers read badly on a real interleaved turn; 11% doesn't buy it up front.

## Server design decisions

- A text delta arriving on a `tool_use` block's index is *tool input being serialised*, not reply text. `content_block_start` tracking by `event.index` is therefore **a privacy control as well as a correctness one**.
- **`partial_text` must NOT live in the `activities` list.** That list is capped (`MAX_TURN_ACTIVITIES = 64`) with FIFO eviction, correct for status labels and catastrophic for reply text, where eviction silently deletes the beginning of the answer. Two lists with different retention because different failure tolerances. It is cleared wherever `activities` is cleared (a coalesced turn showing the previous turn's half-written reply is the bug that prevents), and length-capped defensively (~100KB; see `turn-retention.md`).
- The serializer field is additive and safe to deploy ahead of any client: iOS `Turn` uses explicit `CodingKeys` with a plain `JSONDecoder`, so an unknown key is ignored.
- `parse_activity_line` is documented as pure, total and never-raising, returning at most one event per line; text deltas accumulate rather than describe, so they got their own small function and callback beside it.

**Privacy.** Streaming assistant text does not weaken `ActivityEvent`'s name-only rule. What streams is the model's own reply, the same bytes `result_text` returns to the same client over the same authenticated channel, arriving earlier. No tool input is added anywhere.

**The correctness rule: `result_text` wins.** On terminal status the client renders `result_text`, not its accumulated partial, which is an approximation from deltas that may have been dropped, capped or truncated.

**Amended 2026-09-04 — the reply is delivered whole.** That rule shipped on a false premise: `result` is the *final assistant message's* text, so on the interleaved 11% it is only the reply's last paragraph. Mike caught it from the phone: 751 characters streamed, an `Edit`, then a 27-character confirmation, and on completion the 751 were replaced by the 27. The truncation predates this feature by a year; streaming made it visible. `invoke.py._whole_reply` reassembles the reply from the tracker's text blocks into `result_text`, so the client still renders one authoritative field and needed no change. Two guards make it a recovery rather than a substitution: the tracker's last block must equal `result` (so any change to what `result` means falls through to the envelope), and blocks join with a blank line. Confirmed on the real CLI that evening: `reply spans 2 text blocks; envelope carried the last 484 of 914 chars`, and the phone showed both halves.

**Cron was unfixed, and that was measured, not assumed.** `--output-format json` carries no content blocks, so an unattended turn has nothing to reassemble from. The first draft claimed the morning brief was therefore losing text; Mike pushed back and the box disagreed. Over every non-error invocation in `usage.jsonl`:

| Source | Turns | multi-text (truncatable) | tools then one text | text only |
|---|---|---|---|---|
| cron | 75 | **0** | 2 | 73 |
| ios | 331 | **33 (10.0%)** | 31 | 267 |

A cron prompt is one instruction, so the model does its work and writes one answer (73 of 75 never called a tool). The iOS 10.0% independently reproduces the 10.6% above. **Decision rule:** move cron to stream-json only if a cron turn is ever observed producing more than one text block; `test_the_same_turn_unwatched_still_gets_the_envelope` pins today's behaviour so a change is a decision. *(Superseded 2026-09-28: cron moved to stream-json anyway, for the activity log, `ux-lane.md` §2a, not for truncation.)*

## Decisions made on the phone

- **`fastInterval` while streaming: ANSWERED 2026-09-02. Not the whole phase, just the front of it, and for the STATUS LINE rather than the text.** With the reply typing itself, 2s reads fine; what it costs is the tool label. A tool call starting at t=2.1s is invisible until t=4s, so a fast tool leg can come and go between two polls. A 500ms burst for the first 6s (`PollingPolicy.burstInterval`) puts it on screen within half a second.
  - **It cannot flood a slow link, which is why it needed no adaptive logic.** `pollLoop` is sequential (sleep, await, sleep), so cadence is `interval + round trip` and the interval is a *ceiling on the rate, not a floor*. Good wifi (~30ms): ~0.53s. Bad cell (~1.5s): self-throttles to ~2s, today's behaviour.
  - **Bounded by wall clock, not attempts**: 12 attempts at 500ms becomes 24s of burst when each round trip costs 1.5s.
  - Permissions stay on the fast cadence (a request has 55s to be answered).
- **No status line at all while the reply types itself** (Mike, 2026-09-02): "if the client is rendering typewriter text, there's little point showing a status." The first attempt relabelled `text` events "Writing…" instead of "Thinking…". **That aimed at the wrong moment, and the phone proved it.** `parse_activity_line` emits its `text` event from the *complete assistant message line*, written when the block FINISHES; with `--include-partial-messages` the deltas in between are `stream_event` lines carrying no activity. So throughout the streaming window there are no new activity events and the label sits on the stale `tool_end` "Thinking…". "Writing…" arrives as the block closes, when it stops mattering. What works keys off **what is on screen, not what the last event said**: while streamed text is rendered, a generic label (`TurnActivity.describesUnseenWork` names them) hides the status row. A TOOL label is kept: during an interleaved turn the text stops mid-answer while the model goes back to a tool, and that label is the only thing explaining why. **Under partial-message streaming, activity events are sparse and lag the text; a label that must track what the model is doing right now cannot be built from them.** Also: a connector-named web search fell through to "Using a tool…", the least informative label on the longest leg; now matched on web + search/fetch/browse.
- **What the first phone session showed.** With text on screen the label is a subtitle under something moving, and a fast tool leg (~700ms of "Checking your calendar…") is easy to miss. Mike's read: not worth "fixing" with auto-scroll, which would drag the view past unread text. The burst is the cheap half. If tool visibility during a streamed reply still matters, the hook is `partial_text_marks`, where the chip would be drawn inline.
- **`num_turns` earned its keep on day one.** It is in the result envelope (`usage-telemetry.md` had asked for loop depth across three reads) and is recorded from 2026-09-02. It is the tool-vs-no-tool discriminator: three phone turns on the first evening read 1, 3, 1, so two answered with no tool loop at all. That is the difference between "the status label was too quick to see" and "there was never a label to see".

## Open

- **Does the smoothed cadence feel right?** Felt right on the phone 2026-09-03; do not pre-tune further.
- **Does `--include-partial-messages` change `duration_ms` or the usage counters in the result envelope?** Never checked. A shifted baseline would silently corrupt `usage-telemetry.md`'s trend, now load-bearing for the cost question; compare one turn's telemetry before and after.
