# Task — `backup-identity-bundle.sh` can't actually reach the box

**Status: DONE 2026-10-14 — scheduled, with a staleness alert.** A launchd agent
on the Mac (`scripts/install-backup-launchd.sh`) runs the backup daily and at
login; `scripts/backup-identity-bundle-scheduled.sh` notifies when the newest
bundle is more than 3 days old. First launchd run 2026-10-14: 381 commits,
check clone passed. The notification is seen on screen when launchd fires it
(Mike, 2026-10-14). See "Decision rule" for why the 10-10 park was reversed.

Earlier: **ACCESS GAP CLOSED 2026-08-10 by Mike — option 1 below is what he did.**
The Mac now has key auth as `marlow@`, and the script's *exact* default remote
command was verified from here the same day:
`ssh marlow@marlow.<tailnet>.ts.net 'git -C $HOME/marlow-identity bundle create - --all'`
returns an **86,800-byte bundle**. Both the default `BOX` string and the short
`marlow@marlow` alias authenticate.

**First run end to end: 2026-10-10, by Mike, from the Mac.** Default `BOX`
and `BUNDLE_DIR`; it wrote `~/backups/marlow-identity/marlow-identity-2026-10-10.bundle`
(516K), and the check clone restored 356 commits across three refs (`main`,
`pre-merge-backup`, `origin/main`). Local `main` was one commit ahead of
`origin/main`, a turn made after the hourly push, so the GitHub copy was
current to the hour. Retention pruning is still unexercised (one bundle, keep
14). What keeps this task open is "Also unresolved" below: nothing schedules
it and nothing alerts when it stops.

Originally (2026-08-09, found while walking §7a for the first real push): the
script is written, tested, and merged (2026-08-09), and had never run because the
SSH access it assumes did not exist. The gap analysis below is kept because it
records *why* option 1 was the right one.

## The gap

`scripts/backup-identity-bundle.sh:31` defaults to
`BOX="${MARLOW_BOX_SSH:-marlow@marlow}"` and runs
`git -C $HOME/marlow-identity bundle create - --all` there.

Mike's Mac has key auth to the box **as `mike`**, not as `marlow` — and `mike`
cannot read `/home/marlow` (confirmed 2026-08-09: `git -C
/home/marlow/marlow-server log` from `ssh marlow` returns "Permission denied").
So the default fails, and pointing `MARLOW_BOX_SSH` at the working `mike` alias
fails differently and more quietly: `$HOME` resolves to `/home/mike`, so the
remote `git -C` runs against a path that isn't there.

**Why this matters more than a normal untested script.** This is the half that
makes handing Marlow a write-capable deploy key acceptable
(`docs/tasks/marlow-working-dir-git-policy.md` piece 4, and the pusher's own
module docstring says to read the two as one mechanism). Right now the hourly
push is live and the off-box copy that bounds its blast radius is not. That is
the weaker half of the pair being the one that runs.

## Options, none chosen

1. **Give the Mac key auth as `marlow`.** Add the Mac's public key to
   `~marlow/.ssh/authorized_keys`. Simplest, and matches what the script already
   assumes. Note it puts a second inbound credential on the `marlow` account,
   which is the account the agent runs as — worth a moment's thought about
   whether inbound-to-marlow is a direction anything else cares about.
2. **Keep connecting as `mike` and `sudo -u marlow` remotely.** No new key, but
   `mike`'s sudo prompts for a password, which is exactly what makes it
   unrunnable from a script (and unrunnable through Claude Code's `!` runner —
   no TTY). Would need a NOPASSWD sudoers entry scoped to one command, which is
   its own small privilege decision.
3. **Push-from-the-box instead of pull-from-the-Mac.** Rejected on sight, and
   recorded here so it isn't re-proposed: the direction is the entire point. A
   box that can write to the backup can destroy the backup.

**Option 1 is what happened** (Mike, 2026-08-10). The second-inbound-credential
note above is now a live fact rather than a consideration: `marlow` — the account
the agent runs as — accepts an inbound key from the Mac. That is the direction
this mechanism needs, and it does not give the box anything new.

## Also unresolved

- **Nothing schedules it.** Even once it runs, it runs when Mike remembers.
  `deploy/README.md` §7b says a launchd entry on the Mac is the obvious next
  step and is not built. A backup that depends on being remembered is roughly
  the failure this whole piece exists to fix, one level up.
- **Nothing alerts when it doesn't run.** The pusher reports its failures
  through `/internal/cron/message`; this has no equivalent, and the Mac has no
  channel to report *through*. Related: `docs/tasks/failure-alert-channel.md`.

## Decision rule

**Close option 1 (or 2) before the identity working dir accumulates anything
Mike would miss.** Until then the box's local git plus GitHub is two copies, and
the thing that is actually missing is protection against the agent force-pushing
away the GitHub one — a narrow window, but the one the key was handed over on.

The script has now run by hand (2026-10-10), so the launchd scheduling is
unblocked, but **parked by Mike the same day**: run it by hand for now, and
build the launchd entry when a missed backup is actually felt.

**Reversed 2026-10-14 (Mike):** "how would I notice a missed one?" A missed
backup is only felt at restore time, which is too late, so that trigger could
never fire. Backups are the exception to waiting for a felt symptom. Built: the
launchd agent, plus the staleness notification, which is the part that makes a
silent failure visible. One failed run does not notify (off the tailnet is delay,
not loss); a newest bundle older than 3 days does. The notification is macOS-only
because the Mac has no other channel (`failure-alert-channel.md`).
