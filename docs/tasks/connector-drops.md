# Connector drops — turns that start without Gmail and Calendar

**Status: OPEN, measuring (Mike, 2026-10-17: "I'm not happy that they go
away randomly").** Built 2026-10-18, not yet deployed: cron turns pass
`MCP_CONNECTION_NONBLOCKING=false`, and every streamed turn's activity-log
record carries `mcp_not_connected` (see "What was built"). Next: after a week
or two on the box, compare cron against chat with the scan under "What was
built". Server-side only; neither client shows the field.

## The symptom

A turn's session `.jsonl` gets a `deferred_tools_delta` attachment whose
`removedNames` holds the claude.ai connector tools: all 30
`mcp__claude_ai_Gmail__*`, 9 Calendar and the Claude Docs tools, 44–47 in
total. A later delta's `addedNames` puts them back. While they're gone, the
model can't see mail or calendar at all.

Felt on 2026-10-16: the 16:00 `email-flash` started without them and replied
`[silent]` (4 s, one model turn, no tool call), so Mike got no check. The
Haiku shadow run 6 s later had them (`model-shadow-run.md`, "Day 1").

## How often (all sessions, Sep 17 – Oct 17)

**Not rare: 1–3 turns on most days, 60+ drops in a month.** Re-run the scan
rather than re-deriving the numbers. On the box, as `marlow`:

```python
import json, glob, os
for f in sorted(glob.glob(os.path.expanduser(
        "~/.claude/projects/-home-marlow-marlow-identity/*.jsonl"))):
    for line in open(f):
        try: d = json.loads(line)
        except ValueError: continue
        a = d.get("attachment") or {}
        if a.get("type") != "deferred_tools_delta": continue
        rm = sum("claude_ai_Gmail" in n for n in a.get("removedNames") or [])
        ad = sum("claude_ai_Gmail" in n for n in a.get("addedNames") or [])
        if rm or ad:
            print(d["timestamp"], "DROP" if rm else "back", os.path.basename(f)[:8])
```

Timestamps are UTC. A turn is blind when its DROP has no "back" before the
next turn starts.

- **Usually the tools are back within 2–15 s, in the same turn.** The model
  carries on and uses them, and nobody notices.
- **Eight turns stayed blind until a later turn:** Oct 2 14:00, Oct 8 09:00,
  Oct 9 15:00, Oct 11 15:00, Oct 12 18:00, Oct 13 12:00, Oct 14 09:00 and
  Oct 16 16:00 (MDT). **All eight are `email-flash`.**
- The drops come at turn start, on chat and cron turns alike, and are not
  tied to deploys. The 2026-10-16 06:45 drop that START first noted was one
  of many.

## Leading hypothesis: a startup race, and email-flash bails first

The claude.ai connectors look like they load asynchronously when the CLI
starts. If the first API request goes out before they've arrived, the tool
list lacks them, and they're added a few seconds later mid-turn. A long turn
(chat, morning, consolidate) is still running when they arrive. But the
`email-flash` prompt says *"If Gmail isn't available (check `ToolSearch`),
reply exactly `[silent]`"*, so a flash that sees no Gmail at its first step
quits before they land. That explains why every blind turn is a flash.

**Caught live once, 2026-10-17** (`scripts/probe-drive-connector.sh` as
`marlow`, CLI 2.1.301, ten connectors on the account). In one of four `-p`
runs the stream-json `init` event listed every claude.ai server as
`"status": "pending"` and the session had 13 tools instead of 78. The model
answered "no Gmail tool" without searching. The runs either side showed every
server `connected`. So at least some drops are this: the CLI's first request
goes out while the connectors are still pending. It also gives the harness
something cheap to read: `invoke.py` already parses the stream, and `init`
is its first event.

Still to confirm:

1. **Timing.** On drop turns, compare the CLI's start (the `invoking claude -p`
   log line) with the first API request and the delta that adds the tools
   back. The race predicts the re-add arrives a few seconds after the start.
   A drop with no re-add until the next process start points at the account
   or service side instead.
2. **Rate against how a turn starts.** Fresh process vs `--resume`, the cache
   gap, and time of day. Do drops cluster at :00, when cron fires? That could
   also be load on the claude.ai side.

## Findings, 2026-10-18

- **Nothing keeps init status today.** `invoke.py` drops every `system` line
  (`_NON_ACTIVITY_LINE_TYPES`, and `parse_stream_readings` pre-filters them
  out). But every turn except compaction already reads stream-json (the
  activity log's `on_tool_call`), so the `init` event is in stdout on every
  cron and chat turn. The only per-turn trace on the box is the session
  `.jsonl` delta scanned above. Cheapest capture: count the `claude.ai`
  servers that aren't `connected` at init, in the same pure pass as
  `parse_stream_readings`. Carry the count on `InvokeResult` and write it into
  the activity-log record. No change to argv or env, and the log is
  agent-unreadable by construction (`activity_log.py`).
- **No rise after 4 → 10 connectors.** The scan gives 2 drops on 10-17 and 1 on
  10-18 (to 12:30 UTC), against 1–4 a day since Sep 27. All of them were back
  in 3–15 s.
- **The CLI has the knobs, read statically from the 2.1.301 binary (not run):**
  - `MCP_CONNECTION_NONBLOCKING`: `nonBlocking = env !== false`. Connecting is
    non-blocking unless this is set false, and the claude.ai connectors then
    connect with `deferConnect`.
  - `CLAUDE_CODE_MCP_STARTUP_WAIT_MS`: overrides the first-turn hold's
    deadline (`host_wait`). Without it, a `-p` run with `--mcp-config` (ours)
    already holds up to `MCP_TIMEOUT` (30 s), yet connectors were pending.
    So the hold probably skips the deferred connectors, and this variable alone
    may not help.
  - `CLAUDE_CODE_MCP_CONNECTOR_PREWAIT_MS` and `_PREWAIT_SERVERS` apply only
    to cloud workers (`sdkUrl`).

  `scripts/probe-connector-wait.sh` measures each arm: init status, whether
  Gmail loads, and the first-response latency it costs. **Run 2026-10-18 13:22
  UTC, 2.1.301, 8 runs per arm: inconclusive.** Every one of the 24 runs had
  all 10 servers `connected`, 78 tools and `GMAIL_YES`, so the race never
  fired and neither variable was tested against it. What it does show is
  that both variables cost nothing when the connectors are quick: `t_first`
  was 3.0–4.8 s with no variable, 3.1–4.2 s with `NONBLOCKING=false` and
  2.8–3.8 s with `STARTUP_WAIT_MS`. A wait costs time only when a connector
  is slow, and then it's the wait we want. The 10-17 catch was also on a run
  *without* `--mcp-config`, so the 30 s hold didn't apply there. Probing until
  the race shows burns quota at roughly a 1-in-10 hit rate. Production
  already produces 1–3 drops a day for free.
- **Unattended turns only** (Mike, 2026-10-18). Chat recovers mid-turn, and all
  eight blind turns were cron, so any wait applies to cron only. Chat must not
  pay startup latency for tools it usually doesn't need.

## What was built (2026-10-18)

- `invoke._scrubbed_env(wait_for_mcp=...)`: `source == "cron"` gets
  `MCP_CONNECTION_NONBLOCKING=false`. Any inherited value is dropped, so the
  source alone decides. Compaction and `system` keep the default too:
  compaction's summary blocks the user's next turn. The CLI parses the
  variable as a tri-state boolean (`H.triBool()` in 2.1.301), so `"false"` is
  read as false, not as a truthy string.
- `invoke.parse_init_mcp_status`: the names of the servers whose status at the
  first `init` wasn't `connected`, on `InvocationResult.mcp_not_connected` and
  in the activity-log record. `[]` means all connected; `None` means no init
  was read. A journal warning names them when the list is non-empty.
- **Reading it:** on the box, `~/.marlow/data/activity/<YYYY-MM>.jsonl` (under
  `settings.data_dir`). Count records with a non-empty `mcp_not_connected`
  by `source`. If cron goes quiet while chat keeps showing `claude.ai`
  entries, the variable works. If cron still shows them, it doesn't, and the
  harness retry below is next.
- **Accepted cost:** a cron turn waiting on a slow connector holds the single
  work queue, so a chat turn sent during it waits too. The wait is bounded by
  `MCP_TIMEOUT` (30 s), and so far connectors have been back in 2–15 s.

## Options, cheapest first (not decided)

- **Prompt: don't bail on the first look.** If Gmail is missing, `ToolSearch`
  once more before giving up. That costs one tool call, and on the race
  hypothesis the connectors have landed by then. It's an identity-template
  change (`identity/cron/email-flash.md`), so `update.sh` delivers it. It
  doesn't help if the drop lasts the whole turn.
- **CLI: wait for connectors before the first request.** Check whether the
  CLI has an option or env var that blocks on MCP and connector startup in
  `-p` mode. None is known yet. Remember the "no `--strict-mcp-config`"
  constraint in CLAUDE.md.
- **Harness: detect and retry.** `invoke.py` already parses the stream. A
  cron turn whose session gained a claude.ai removal, and that ended without
  a re-add, could be re-run once. This is more code in the most sensitive
  module, so only do it if the prompt fix isn't enough.
- **Surface it.** If a check really ran blind, put that in the transparency
  sheet, never in the chat. Mike wants no diagnostics in the message flow.

## Not this

- The connectors *missing entirely* under a `claude setup-token` token
  (`marlow-for-taylor.md`) is by design: that token grants inference only.
- Marlow's own account of its tools is not evidence (START, "Don't believe
  Marlow about its own harness"). Use `scripts/probe-connectors.sh` or the
  session files.
