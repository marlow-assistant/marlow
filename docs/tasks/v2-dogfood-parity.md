# v2 dogfood parity — replacing v1's cron jobs

**Status: DONE.** All six stages shipped; v2 has Mike's mornings. v1 is shut
down (`systemctl --user disable marlow`, crontab cleared). Rollback stays
available: `systemctl --user enable --now marlow` +
`crontab ~mike/claude-assistant/crontab-v1-backup.txt`.

**The live crontab** (re-confirmed 2026-08-10 — trust this over anything
below, including the schedule times, which moved during rollout):

| when | job |
|---|---|
| hourly at :17 | `identity-push.py` (`marlow-working-dir-git-policy.md` piece 4) |
| 07:20 daily | `trello-sync.sh` (root-owned, `/opt/marlow/bin/`) |
| 07:30 daily | `morning` |
| Mon 07:33 | `alberta-weekly` |
| Sun 18:00 | `weekly-digest` |
| 23:55 daily | `consolidate` (`nightly-consolidation.md`) |

**Two decisions this doc used to carry as open, both resolved and now in
CLAUDE.md's hard-constraints section — don't re-derive them here:**
`--strict-mcp-config` was dropped 2026-08-06 (it stripped the Gmail/Calendar
connectors v1's morning jobs depend on); `AGENT_CONFIG_PATHS` is what replaced
it as the write-time guard.

## What v1 actually was, worth keeping since it's not derivable from git

Gmail and Calendar arrive as **account-level Claude.ai connectors**
(`mcp__claude_ai_*`), not an MCP server config — v1 had none. `cron/entrypoint.py`
reads `$MARLOW_WORKING_DIR/cron/<job>.md` and POSTs it to
`/internal/cron/message`, so porting a v1 job is a prompt file plus a crontab
line, no code. `trello-sync.sh` is the one exception: pure `curl`+`python3`,
no LLM, so it isn't an `entrypoint.py` job — it stays marlow-editable rather
than getting the root-owned `/opt` treatment, a deliberate call by Mike
2026-08-07 (full reasoning in `marlow-working-dir-git-policy.md` → "Resolved
2026-08-07") so that Marlow can keep maintaining a script it authored under v1
without requiring Mike at a terminal for every change.

**Methodological finding worth keeping for the next connector-shaped
question:** a model is not a reliable witness about its own toolset — two
enumeration attempts both answered "no connectors," including one from a turn
that then successfully called one. Verify by grepping `tool_use` events in the
CLI transcript, never by reading the reply.

**Debt still open:** `EGRESS_ALLOWLIST` doesn't contain the connector MCP
hosts. Harmless while §9 egress stays deferred/unenforced; add the hosts as
part of that work, not before, or Gmail/Calendar will look like an auth
failure the day `egress.nft` lands.

Full stage-by-stage rollout history, probe baselines, and the runbook that was
actually run on the box are in git history (this doc, pre-2026-08-10) if ever
needed — none of it is live reference material now that all six stages shipped.
