# Usage visibility — plan usage and conversation size in the transparency sheet

**Status: iOS SHIPPED 2026-10-05** (server half, iOS panel and the idle-compaction
gate all merged that day). **Web half merged 2026-10-09 and live on
Mike's box; not yet seen on the Android user's phone.** The Android user couldn't find the panel, which is what
found the gap. The web port is `usageFormat.ts`, `UsageFormat.swift` line for line
with its tests: a Usage panel in the Marlow sheet, and the row as the strip's
lowest banner (STA-2). Open: the ≥90% row has not been seen on screen on either
client.

iOS half built 2026-10-04, branch ios-usage-sheet (Usage panel in the
sheet; yellow row in chat). Deviations: the row is a `ChatStatus.usageHigh` case
(lowest precedence, black text on yellow) fed by `ChatStore.usageReport`, read in
the health loop on each run's first pass and then at most every 5 min
(`PollingPolicy.usageInterval`), after the connection state settles; a failed
read keeps the old value and never touches reachability. A retired conversation
shows only its "fresh conversation" line (size, start and tidy-ups hidden). A
plan window past its reset reads "N% at last reading · has since reset" with an
untinted bar. Web half not started.

First device look 2026-10-05 (Mike): plan figures match Claude's own usage
panel (5-hour 3% vs its 2%, a rounding difference he waved off; weekly 13% and
both resets identical); a fresh conversation reads ~38K. Changes from that
look: the section header names the plan ("Claude Pro"/"Claude Max", fallback
"Your Claude plan") from a new top-level `subscription_type` (the raw
`subscriptionType` auth_monitor already parses from the CLI login); the "as of"
time moves into that header; the footnote says Marlow shares the plan's limits
with everything else; "Marlow's part" becomes "Marlow's turns" with the
"Mostly chat" line dropped (it counts turns, it cannot give a share of the
plan); after a tidy-up the size line says so instead of reading like a bug.
The same look found the idle-compaction gate compacting a 38K session on its
summed reading, fixed separately (branch `compaction-true-size`). The ≥90% row
is still unseen.

Server half built 2026-10-04, branch usage-endpoint (`GET /v1/usage`). Deviations from the design: readings are
parsed from the finished stdout (`invoke.parse_stream_readings`), not in the
live line handler, so a failed-exit turn still yields one; compaction turns run
on plain json and so carry no readings; a stale plan window (`resets_at` in the
past) makes `marlow` fall back to the rolling now-5h / now-7d span; only the live
`usage.jsonl` is read, not rotated siblings. After `/code-review high`: a
successful compaction sets `context_tokens` to null (size unknown until the next
chat turn) rather than leaving the pre-compaction number; `compactions` counts
successes only; `conversation.retired` is true once the nightly rollover has
retired the session. **Unverified on a real stream:** the assistant line's
`message.usage` and top-level `parent_tool_use_id` shapes (no fixture has
them) -- check `context_tokens` in `usage.jsonl` after the first deploy.
Supersedes the "manual compact /
start fresh, keep memory" idea, which no felt symptom supports yet: on the long
casual day nothing went wrong, Mike just couldn't *see* how much he'd used.
Ship visibility first; if he then keeps wanting to wipe a conversation, that is
the symptom that releases a reset button.

## What Mike wants to know (2026-10-04)

1. **Am I about to run out?** The 5-hour and weekly subscription windows.
2. **How much of that is Marlow?**
3. **How big has this conversation got?**

Placement rule: all of it lives in the transparency sheet, **nothing in the
message flow**. Mike: "I want fewer excuses to not use Marlow, so anything
off-putting is bad." One exception, his: **≥90% on either window** earns a
yellow line on screen, because running out is a hard stop, not a nudge.

## 1. Plan usage — free, already arriving

Every `claude -p` stream carries a `rate_limit_event` line (CLI 2.1.284, run on
the box 2026-10-04; on 2.1.235 it was the first line, now it comes after
`system/init` — position is not part of the contract):

```json
{"type":"rate_limit_event","rate_limit_info":{"status":"allowed",
 "resetsAt":1791169200,"rateLimitType":"five_hour","overageStatus":"allowed",
 "isUsingOverage":false,
 "unifiedWindows":{"five_hour":{"utilization":0.14,"resetsAt":1791169200},
                   "seven_day":{"utilization":0.08,"resetsAt":1791630000}}}}
```

`invoke.py` currently skips it (`_NON_ACTIVITY_LINE_TYPES`). Capture
`unifiedWindows` plus `status` and the time observed; keep the latest in
memory. Parse defensively: unknown or missing keys mean "no reading", never an
error. The line is undocumented and has already moved once.

`claude -p "/usage"` also works headless, with no model call (370 ms,
zero tokens), and returns the same two percentages as prose. It is not
the source: the stream line is structured and free. It is a possible refresh
path, see the open questions.

**Staleness is the honest caveat.** The reading is as of Marlow's last turn.
Mike's usage on other surfaces (Claude Code on the Mac, claude.ai) moves the
same windows without Marlow seeing it. So send `observed_at` and let the sheet
say "as of 14:32". Cron turns refresh it several times a day for free.

**Resolution looks like whole percent** (0.14, 0.08). Fine for a bar and the
90% line, too coarse for per-turn attribution (see 2).

## 2. Marlow's share — the hard one

The windows are the whole subscription, all surfaces. Measuring Marlow's
share directly isn't possible:
- Per-turn deltas (reading at this turn vs the next) include whatever Mike did
  elsewhere in between, and whole-percent resolution rounds most single turns
  to zero.
- `/usage`'s "local sessions on this machine" breakdown is box-only, so
  Marlow-only, but it gives behaviours (long sessions, top MCP servers), not
  a share.

**v1 does not claim a percentage.** Next to the plan bars it shows Marlow's
own activity in the same windows: turn count and a chat vs scheduled-jobs
split, from `usage.jsonl`, weighted so cache reads don't dominate. That
answers "is it the morning jobs or me?" without inventing a number. If a real
share is still wanted after living with it, a calibration (utilization movement
over stretches when only Marlow ran, against Marlow's weighted tokens) is the
next step. Don't build it speculatively.

## 3. Conversation size — needs a new, true number

**Do not display the idle-compaction gate's number.** `_sum_prefix_tokens`
sums usage across the turn's internal tool loop, so it reads 1–4× the real
size and swings wildly between consecutive turns (2026-10-03: 318,927 →
213,650 → 77,342 in six minutes, `scripts/context-curve.py`). That bias is
why that was once deliberate is in `idle-compaction.md`, "Read 2026-08-23";
on screen it would be nonsense that teaches Mike to distrust the sheet.

The true size is the **last model call's** prefix
(`input + cache_read + cache_creation` on the final `assistant` message's usage
in the stream), which `invoke.py` already parses line by line. Capture it
It was specced display-only here; on 2026-10-05, after the summed number
compacted a 38K session, the gate moved onto it too (`idle-compaction.md`
status block).

Show it against something meaningful rather than as a bare token count:
the compaction threshold (110K), when this conversation started (since the
overnight rollover), and how many compactions it has had. Wording and scale
are the phone's business.

A free side effect: the first real curve of conversation size, which is what
would show whether 110K was ever the right threshold (never re-checked; the
2026-08-28 read didn't happen). Record it in `usage.jsonl` as its own field
next to the summed one.

## Shape

- **Server:** capture both readings in `invoke.py`, hold the latest plan reading
  and the attended session's true size, and serve them on a new authenticated
  `GET /v1/usage`. Facts only: utilization fractions, reset epochs, observed_at,
  status, conversation size and threshold, session start, compaction count,
  today's chat/cron split. No wording, no thresholds for display.
- **iOS:** a section in the transparency sheet, and the ≥90% yellow line. The
  90 lives on the phone (`feedback`: facts on server, wording on phone).
- **Web:** same section; its card can follow.
- **Identity:** nothing for Marlow to learn; the agent doesn't read this.

## Open questions

- **Where exactly the yellow line goes.** Header area, not a modal or a chat
  row. Decide when building, with Mike looking at it.
- **Refreshing a stale reading** when the sheet opens. `/usage` headless would do
  it, but it is a `claude -p` call, and every one goes through the single work
  queue. Probably not worth it given cron refreshes; revisit if "as of" is ever
  hours old when he looks.
- **`status` other than `allowed`** (presumably a warning, then `rejected`)
  hasn't been observed. Pass it through; if it's ever `rejected`, the turn
  failing is the louder signal anyway.
