# Connector startup latency — snappy turns without giving up connectors

**Status: BACKLOG, not started (Mike, 2026-10-21).** Mike disconnected the
claude.ai Trello connector from the website because it was adding about 30 s
to CLI startup: "unacceptable for Marlow. But I want to be able to have
connectors like Trello." Nothing measured yet. First step is the probe under
"Next". Server-side only. No client change unless a later option adds a
user-facing setting, and then both clients need it. Which connectors Marlow
should use at all is a separate feature review, `connector-policy.md`.

## The tension

Every claude.ai connector on Mike's account loads into every `claude -p` turn
Marlow runs (gate #1's premise; `marlow-tool-surface-trimming.md` §1). So one
slow connector taxes every turn, including the ones that never use it. The
obvious scoping flag, `--strict-mcp-config`, is off the table: it also strips
Gmail and Calendar (CLAUDE.md, `v2-dogfood-parity.md`).

`connector-drops.md` pulls the other way. Its fix makes cron turns *wait* for
connectors (`MCP_CONNECTION_NONBLOCKING=false`), because `email-flash` ran
blind eight times when Gmail arrived late. So "start fast" and "have the
connector when you need it" are the same knob, turned opposite ways for two
kinds of turn.

## What we already know

- **Denying a connector does not stop it connecting.** Trello has been in the
  `deploy/marlow-settings.json` deny list since 2026-08-24, so its tools
  never appeared in Marlow's listing. It still cost the startup time. A
  `--disallowedTools` / deny entry is a listing filter, not a connection
  filter. Don't propose it as the fix.
- **30 s is exactly `MCP_TIMEOUT`.** `connector-drops.md` ("Findings,
  2026-10-18") read from the 2.1.301 binary that a `-p` run with
  `--mcp-config` (ours, for the weather server) holds the first turn up to
  `MCP_TIMEOUT`, 30 s by default. That doc guessed the hold skips the
  deferred claude.ai connectors. A 30 s lag from a slow Trello suggests that
  guess is wrong, at least for some connectors. This is a hypothesis.
- **Unknown: which turns paid it.** Chat keeps the non-blocking default and
  cron waits. If chat paid 30 s too, the `--mcp-config` hold is the likely
  cause, not the cron variable. The activity log's `mcp_not_connected` field
  (live since the connector-drops change) and turn durations should tell
  them apart. Check whether Trello shows up there before its removal.

## Options (not decided)

Cheapest first:

1. **Cap the wait.** `CLAUDE_CODE_MCP_STARTUP_WAIT_MS` overrides the
   first-turn hold's deadline (`connector-drops.md`). For chat, a short cap
   (say 3–5 s) bounds the worst case. Gmail and Calendar usually connect in
   that time, and a late connector arrives mid-turn anyway. Cron could keep a
   long wait. One env var per source in `invoke._scrubbed_env`, the same shape
   as the existing cron switch.
2. **Lower `MCP_TIMEOUT` for chat only.** Cruder than (1). It may also cut
   off a connector that is slow but needed.
3. **Drop the hold's trigger.** If the hold comes only from `--mcp-config`,
   check whether the weather server can be supplied another way that doesn't
   arm it. Keep in mind the `AGENT_CONFIG_PATHS` constraint: MCP config files
   must stay unwritable by the agent.
4. **Per-turn connector selection.** Load only the connectors a turn needs.
   There's no CLI mechanism for this short of `--strict-mcp-config`, which
   takes all claude.ai connectors with it. Revisit only if the CLI gains a
   per-server include or exclude for claude.ai connectors. Check the
   changelog on each deliberate upgrade.
5. **Account split.** Connectors Mike uses himself go on a different account
   from the one Marlow runs as. That costs a second subscription
   (`marlow-tool-surface-trimming.md` §1), so it's a last resort.

The likely answer is (1) for chat, plus whatever (3) turns up. Cron keeps
waiting because it isn't latency-sensitive and needs Gmail.

## Next

- Hand Mike a clean-shell probe (never nest `claude -p` here). With Trello
  reconnected on the website, run as `marlow`, eight runs per arm:
  default, `CLAUDE_CODE_MCP_STARTUP_WAIT_MS=3000`, `MCP_TIMEOUT=5000`. Record
  `t_first`, init status per server, and whether Gmail loads. Extend
  `scripts/probe-connector-wait.sh` rather than writing a new script. Also run
  one arm without `--mcp-config` to test the hold hypothesis.
- Pull turn durations around Trello's removal from the activity log, split
  by `source`, to see which turns paid.

## Done when

Trello (or any connector Mike adds) is back on the account. A chat turn's
time to first response doesn't move by more than a couple of seconds with it
connected. Cron still gets Gmail on the first look. And the connector-drops
measurement isn't confounded: if this changes chat's wait, note it there,
because chat is that doc's comparison arm.
