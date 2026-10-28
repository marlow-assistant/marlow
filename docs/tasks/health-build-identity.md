# Task — `/health` should say what code is actually running

**Status: SHIPPED — merged 2026-08-25 and live on the box.** `/health` carries `git_branch` and
`git_commit`, frozen at import (`server/health.py`). Corrected 2026-09-25: this
line said "awaiting review" for a month, and START then concluded from it that
the work was lost. Raised
by Mike 2026-08-22, after asking "what's actually deployed on the server?" and
finding that answering it took three separate SSH queries and a timezone
conversion.

Small and self-contained — a good pick-up task. The design is settled below;
build it as written or say why not.

## The question `git status` does not answer

On the box, `git -C ~/marlow-server rev-parse HEAD` tells you **what is on
disk**. `/health` would tell you **what is running in the process**. Python
resolves modules at import, so the two diverge the moment a merge lands without
a successful restart — and `deploy/update.sh` merges at line 55 and restarts at
line 112. Anything that fails in between leaves git confidently reporting a
commit the interpreter has never executed.

That failure mode is not hypothetical in this repo:

- `update.sh:63` documents the merge rewriting the running script underneath
  itself — found 2026-08-09, a step silently skipped and the run reporting
  success.
- `update.sh:36` documents the checkout sitting on `ios-permission-round-trip-ui`
  for weeks while deploying main's code — "the right code under the wrong
  label," found by accident 2026-08-06.

Both are the same shape: the label and the behaviour drifted apart, and nothing
was watching the gap.

### What it cost to check by hand, 2026-08-22

| | |
|---|---|
| box `HEAD` | a commit on `main` |
| that commit's date | 2026-08-20 05:43 MDT (stamped `-0300`) |
| service `ActiveEnterTimestamp` | 2026-08-20 05:45:59 MDT |

Restart lands 2m34s after the commit, so the process really was running
that commit — consistent, this time. Establishing that took `git rev-parse`,
`git log -1`, `systemctl show`, and converting between two timezones. The
endpoint can just say it.

## What to build

Mirror iOS, which already solves this: `ios/Marlow/Marlow/Views/SettingsView.swift:59`
reads `GitBranch` / `GitCommit` from `Info.plist`, baked in **at build time**.
Use the same two field names on the server so the two surfaces read alike.

Add to `server/health.py`'s `get_health()`:

```
"git_branch": <str>,
"git_commit": <str>,   # short sha
```

**Capture once at process start, into a module-level constant. Do not shell out
per request.** This is the whole point of the task, not an optimisation:

- A `git rev-parse` at request time reports **the checkout**, which is the thing
  that lies. An endpoint that re-reads the disk confirms the fiction instead of
  catching it. The value must be frozen at import, so that only a restart can
  change it — a restart being exactly the event that makes the process match the
  disk.
- `/health` is on a timer from both `deploy/watchdog.sh` and the dead-man's
  switch. Keep a subprocess spawn off that path.

Read it from git at import (the box always has a real checkout), and fall back
to `"unknown"` rather than raising — `/health` is the wedge detector, so it must
never fail to answer because a git call did. Match iOS's `?? "unknown"`.

Emit `git_branch` even though `update.sh:43` refuses to deploy off `main`, so it
is nearly always `"main"`. The case where it is not is precisely the 2026-08-06
bug, which is the one worth shouting.

## Tests

`tests/test_health.py` — the fields are present, are strings, and survive a git
call that fails (patched to raise → `"unknown"`, not an exception). Assert the
value is captured at import, not per call, since that is the property the whole
design rests on.

## Two things this does not fix

- **The out-of-checkout copies.** `/opt/marlow/cron/entrypoint.py` and the
  identity pusher are `install`ed outside the repo (`update.sh:89`, `:99`).
  Neither git-on-the-box nor a process-start constant in the server can see
  them, so if cron misbehaves this endpoint will not explain it. Left alone
  deliberately; reopen if a cron-staleness bug ever costs real time.
- **Disclosure.** `/health` is unauthenticated — Tailscale-gated only, no
  `require_device_token` (`server/app.py:293`). This puts a private-repo commit
  sha on an open endpoint. Accepted on a Tailnet, and recorded here so it is a
  decision rather than an oversight. If `/health` ever becomes reachable from
  anywhere else, revisit this line first.

## Reference

`server/health.py`, `server/app.py:293` (public) and `:490` (internal — same
handler, so both get the fields), `deploy/update.sh`,
`ios/Marlow/Marlow/Views/SettingsView.swift:58-64` (the pattern being mirrored).
