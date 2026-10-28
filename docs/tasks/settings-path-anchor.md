# Single-slash path rules in marlow-settings.json are inert

**Status (2026-10-20):** done. Merged 2026-10-20 and deployed to Mike's and
Taylor's boxes, verified. All 17 rules are now `//home/marlow/...` (Mike: "yes
ok"), and `test_path_rules_are_not_single_slash_absolute` refuses a single-slash
absolute.
Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

## Finding

Every path rule in `deploy/marlow-settings.json` was written `/home/marlow/...`,
one leading slash. In a user settings file that doesn't mean the absolute path. The
permissions docs (read 2026-10-19): in `~/.claude/settings.json` `/path` resolves to
`~/.claude/path`; in `--settings <file>` to `<directory of file>/path`. `//path` is
filesystem-absolute and `~/path` home-relative ("a pattern like `/Users/alice/file`
isn't an absolute path"). So `Edit(/home/marlow/.claude/**)` matched
`/home/marlow/.claude/home/marlow/.claude/**`, which is nothing.

`scripts/probe-path-rule-anchor.sh` confirmed it on the box (CLI 2.1.301, as the
admin user, a settings file placed in `~/.claude/` so it gets the same anchor). All
four controls passed:

```
none          unchanged  denials=1 ['Edit']
allow-dbl     EDITED     denials=0 []        Edit(//abs/file) allows
allow-sgl     unchanged  denials=1 ['Edit']  Edit(/abs/file) allows nothing
anchor-moves  EDITED     denials=0 []        `/` anchors at the file's dir
deny-dbl      unchanged  denials=1 ['Edit']  Edit(//abs/file) denies
deny-sgl      EDITED     denials=0 []        Edit(/abs/file) denies nothing
rdeny-dbl     not-leaked denials=1 ['Read']  Read(//abs/file) denies
rdeny-sgl     LEAKED     denials=0 []        Read(/abs/file) denies nothing
```

Not proven: that the CLI has no special case for the literal
`~/.claude/settings.json`. The docs give it the same rule and `anchor-moves` shows
the anchor follows the file's directory, but the real file wasn't swapped (that
would mean editing a live user's settings).

## What was inert

**All 14 absolute path denies** (five `Read(...)`: credentials, `.ssh`, `data`,
`apns`, `tls`; nine `Edit(...)`: `.claude.json`, `.claude/**`, `.mcp.json`,
`claude-assistant/.claude/**`, `.git/**`, `CLAUDE.md`, `FIRST-RUN.md`,
`cron/email-flash.md`, `cron/ideas.md`) and **all three path allows**
(`Read(claude-assistant/**)`, `Edit(claude-assistant/memory/**)`,
`Read(.marlow/attachments/**)`). Not affected: `Read(**/.env)`/`.env.*`
(cwd-relative, so only under the working tree), Bash rules, tool-name rules, the
hook command.

**Nothing was open, and nothing ever had been,** because the hook vets these paths
on its own: `boundary_deny_reason` hard-denies secret, agent-config and
product-owned paths, `file_policy.py` grants the memory and attachment allows
itself and its decision wins anyway, and bwrap tmpfs-hides `SECRET_PATHS`. Without
the hook, an unallowed Read outside the cwd or any Edit still auto-denies headless.

What was lost is the stated defense in depth: the deny comment says the entries are
"duplicated here rather than left to the hook because gap #7 was exactly a box where
the hook was never wired at all". They never were that duplicate. **The fifth case
of the file's own lesson: an inert deny is silent**, and the CLI gives no warning for
this one, so "verify by warning" can't catch it.

## Fix

Respell as `//home/marlow/...`: the smallest change, and `update.sh`'s
`s|/home/marlow/|/home/$1/|g` still matches as a substring, so second instances keep
working. (`~/...` is cleaner but changes more lines in the lockstep test, which
hard-codes the literals.)

Making the denies live changes behaviour in one place: `Edit(//home/marlow/.claude/**)`
is broader than the hook's `AGENT_CONFIG_PATHS` (settings.json, settings.local.json,
plugins/, `~/.claude.json`), so an Edit elsewhere under `~/.claude` that used to
reach the phone is now denied outright. Nothing legitimate writes there (auto-memory
is off, `invoke.py` `_FORCED_ENV`). Making the allows live changes nothing, since
the hook decides.

The admin user's own v1 `~/.claude/settings.json` has the same single-slash spelling
and is worse off: a blanket `allow Read` and no hook, so its `deny Read` of
credentials and `.ssh` were the only thing between v1 and those files, and inert.
That file isn't in this repo; Mike respelled it by hand on the box (2026-10-20). Its
`allow Edit(...claude-assistant/**)` is also inert, but respelling it *widens* v1, so
it is left for Mike to decide.

## Deploy and verify (results)

The deny half is live and a deny beats a hook allow, so the watch was for a denial
that wasn't there before.

- **Mike's box, first.** `update.sh` logged `policy CHANGED`; the live file has 7
  `Read(//home/marlow` rules (five denies plus the two read allows) and 10 `Edit(//`
  rules, no single-slash ones.
- **The rule binds, without a phone prompt:** `scripts/verify-live-policy-denies.sh`,
  passed 2026-10-19 on CLI 2.1.301. `--settings` adds to the user file instead of
  replacing it, so the script uses `--setting-sources project` from an empty cwd
  with a copy of the live policy and no hooks. Both controls went through (a read
  under $HOME, an edit with acceptEdits) and both denies held (the
  `~/.ssh/known_hosts` read and the `CLAUDE.md` edit were refused, unchanged).
- **A normal day, passed 2026-10-20** (deploy 2026-10-19 20:25 to 13:30 next day):
  five denials, each the hook or the phone deciding, not settings (a calendar prompt
  timed out, consolidate's two unattended Greps, one Bash Mike declined). The writes
  the policy must allow all went through (ideas.json, four memory files,
  schedules.json) and every cron job fired. Nothing in the log was a settings-only
  denial, i.e. one with no hook decision next to it.
- **Taylor's box only after that day was clean.** Passed 2026-10-20: 7
  `Read(//home/taylor` rules, 0 `/home/marlow`.
