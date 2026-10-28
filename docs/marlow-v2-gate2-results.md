# Marlow v2 — Gate #2 Results (Permission Audit)

**Date:** 2026-07-18
**Scope:** §5.4/§5.1 of the implementation plan — `fewer-permission-prompts` skill run
against real transcripts, then the layered filesystem model applied to the live
interactive `~/.claude/settings.json`. Independent of gate #1; ships value now
regardless of the server build.

## 1. `fewer-permission-prompts` scan

Scanned the 50 most-recently-modified transcripts across all projects. Result:
**nothing to add.** Everything above the 3-hit threshold is already covered —
`git add`/`commit`/`push` (22 hits) in `.claude/settings.json` /
`settings.local.json`, `mcp__plugin_discord_discord__reply` (51),
`Gmail__search_threads` (12), `fetch_messages` (11) in the global
`~/.claude/settings.json`. Everything else (`grep`, `ls -la`, `find`, `cat`) is
either already auto-allowed by Claude Code's built-in read-only validator or
under the count threshold. `Gmail__get_thread` correctly stays in `ask` — that's
the privacy-intent gate (design §9.1's point that sandboxing covers blast
radius, not whether Marlow *should* read something), not a prompt-fatigue
problem to optimize away.

**Conclusion:** the allowlist was already well-tuned; the fewer-permission-prompts
pass alone doesn't justify gate #2 as a standalone step. The layered deny model
below is the actual content of this gate.

## 2. §5.1 layered filesystem model — applied to `~/.claude/settings.json`

**Removed:**
- `Bash(rm /home/mike/claude-assistant/**)` from `allow` — Mike's call: even
  though scoped to the (git-recoverable) working tree, this is exactly the
  "destructive op" §5.1 says shouldn't be a standing allow. `rm` now falls
  through to the default interactive prompt every time.

**Added `permissions.deny`** (new key — none existed before):
```json
"deny": [
  "Read(/home/mike/.claude/.credentials.json)",
  "Read(/home/mike/.ssh/**)",
  "Read(**/.env)",
  "Read(**/.env.*)",
  "Bash(cat /home/mike/.claude/.credentials.json*)",
  "Bash(cat /home/mike/.ssh/**)",
  "Bash(git push --force*)",
  "Bash(git push -f*)"
]
```
Verified concrete targets before writing the rule: `~/.claude/.credentials.json`
(OAuth token store), `~/.ssh/{authorized_keys,known_hosts}`, and two live `.env`
files (`~/claude-assistant/.env`, `~/.claude/channels/discord/.env` — the
Discord bot token). `deny` wins over `allow` regardless of scope, so this also
neutralizes the project's `Bash(git push *)` allow in `.claude/settings.local.json`
for the `--force`/`-f` shapes specifically, without touching the rest of that
rule.

**Known gap, not fixed here (matches plan §5.1's own caveat):** settings.json
patterns are prefix matches, not argument-aware. The `cat`/`git push` denies
above only catch the flag in the position tested (`--force` or `-f` immediately
after `push`; secret path as the sole/leading argument to `cat`). They don't
catch `git push origin main --force`, `less`/`sed`/`python -c` reading the same
paths, or any other reordering. `find … -exec`/`-delete` needed no new rule —
Claude Code's built-in read-only validator already excludes those flags from
auto-allow, so they already fall through to a prompt. Real argument-shape
enforcement (the thing that closes all of these at once) is the PreToolUse hook
in `boundaries.py`, which is gate #3/#4 work, not expressible in settings.json.
This gate ships the coarse net now; the hook is the actual fix.

## 3. Gate #1 §7 incidental finding — now fixed

Gate #1 flagged an ineffective rule: `Write(/home/mike/claude-assistant/**)` sat
alongside `Edit(/home/mike/claude-assistant/**)` in `allow`, but Claude Code
warns `Write` isn't matched by file-permission checks — `Edit` rules already
cover all file-editing tools, including `Write`. This was missed in the first
pass of this gate (the write-up focused on the new deny layer and didn't audit
existing allow entries for staleness). Removed the redundant `Write(...)` line;
`Edit(/home/mike/claude-assistant/**)` is the only rule needed.

## 4. Not touched
- `.claude/settings.json` / `.claude/settings.local.json` (project-scoped) —
  left as-is; the new global deny rules are enough to close the credential/secret
  exfiltration path without editing project files.
- Everything in `permissions.allow` besides the one `rm` removal.

## Next
Gate #3 — server skeleton (queue, invoke, app, health, boundaries, log),
liveness, Tailscale ingress + pairing. `boundaries.py` should encode the same
concrete paths verified here (credentials, ssh, `.env` files) plus the
argument-shape checks settings.json can't do, per implementation plan §5.
