# Same-turn feedback when Marlow writes an invalid store

**Status: PARKED (Mike, 2026-10-19).** Proven feasible, not built. Not a
major problem today: the store format rarely changes, and Marlow keeps to it
well. Release it when a refused store costs something real: a job that misses
its run because the fix only came a turn later, a store left broken across a
quiet stretch, or a format change (a new key, like `effort`) that makes
mistakes likelier.

## The gap

`cron/schedules.json` and `ideas/ideas.json` are validated by the server, not
by the Edit tool. The Edit reports success either way, and Marlow learns the
store was refused only from the WARNING in the *next* turn's context block
(`server/turn_context.py`, `schedules.rejection_reason`, `ideas.rejection_reason`).
Until then one bad entry freezes the whole crontab at its last good version.

Prompted 2026-10-19: a branch mix-up deployed code without per-entry `effort`
to Mike's box. The old validator rejects `effort` as an unsupported key, so
the whole store froze, and the next-turn WARNING is most likely how Marlow
noticed. It removed the keys, which is the right repair. When Mike asked for
them back, it restored them and said that "cron isn't
server validated".

That's wrong, but understandable from where Marlow sits. The identity says
"the write still succeeds, so you cannot tell by reading the file back".
Marlow read that as no validation at all, when it actually means the
feedback comes a turn late. The update.sh rollback guard (2026-10-19) is the
fix for the mix-up itself; this doc covers only the feedback delay.

## What the probe showed

Running it took four tries, and each failure is now a guard in the script
rather than a false verdict:

- `ssh host 'cmd'` skips the login profile, so `claude` wasn't on PATH.
- mike's OAuth login had expired (credentials last written 2026-08-30). A failed login still
  emits a result event (`is_error: true`).
- `Edit(/home/mike/...)` in an inline `--settings` allow list still
  auto-denied the edit. The probe now uses `--permission-mode acceptEdits`.
  Whether a single leading `/` means "relative to the settings file", and so
  whether `deploy/marlow-settings.json`'s own path rules match anything, was
  split off to its own investigation the same day.

Results:

`scripts/probe-posttooluse-feedback.sh`, run on the box as `mike`, CLI
2.1.301 (results are in its header):

- A PostToolUse hook fires on Edit under headless `-p`.
- All three output shapes reach the model in the same turn: stdout
  `{"decision": "block", "reason"}`, stdout
  `hookSpecificOutput.additionalContext`, and exit 2 with stderr.
  `additionalContext` and exit 2 led it to repair the file. `block` led it
  to ask first, because the prompt had asked for the bad key.
- The hook text is **not** in the stream-json transcript, so the server
  can't see what the model was told. If that belongs on the transparency
  sheet, the hook has to record it.

## The shape, when it's built

- `hooks/posttooluse.py`, wired in `deploy/marlow-settings.json` next to the
  PreToolUse entry, with matcher `Edit|Write|MultiEdit`.
- It acts only when `tool_input.file_path` resolves to the schedules or
  ideas store, and calls the existing `rejection_reason` for each. No new
  rules, and no HTTP to the server: it is a local file check.
- Emit `additionalContext`, exit 0. That matches `hooks/pretooluse.py`'s
  "decision in the JSON, always exit 0" convention, and in the probe it got
  the repair without the model deferring.
- Keep the next-turn WARNING. It still catches writes that bypass
  Edit/Write (a Bash redirect) and anything a hook miss lets through.
- Identity, in the same PR: `identity/CLAUDE.md`'s "the write still
  succeeds, so you cannot tell…" becomes "the edit comes back with the
  error; fix it before anything else". See CLAUDE.md, "changed a harness
  affordance".
- Tests: the hook's file-path match (including a path outside the working
  dir that merely ends in `schedules.json`), its silence on a valid store,
  and its output shape. A settings lockstep test, if the new entry needs one.
- Re-run the probe after any CLI upgrade before relying on it. Hook output
  handling is a property of the CLI version.
