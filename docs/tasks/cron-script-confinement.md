# Task — run promoted cron scripts inside bwrap

**Status: NOT STARTED — captured 2026-08-08.** Future work, explicitly. The
promotion convention (`deploy/README.md` §6b) shipped without this and is
correct without it; this closes the asymmetry the convention documents.

**Two parked items live here now**, both on the promoted-script surface and each
with its own decision rule: *confining* a promoted script (the original, below)
and *scheduling* one from `schedules.json` (added 2026-08-30). Neither blocks
the other.

## The gap, stated precisely

`server/invoke.py` spawns `claude -p` inside a bubblewrap namespace where
`boundaries.SECRET_PATHS` are tmpfs and simply do not exist (probe CONFIRMED
2026-08-06, `scripts/probe-bwrap-confinement.sh`,
`docs/tasks/schedules-and-reminders.md` (the renderer that owns the crontab).. That is why
`server/bash_policy.py` can describe itself as "latency management with a safety
net" rather than the boundary — a miss there costs a prompt, not a key.

A **promoted script** invoked from the crontab gets none of it. cron execs
`/opt/marlow/bin/foo.sh` as `marlow`, directly. `~/.ssh`, the CLI's
`.credentials.json`, and everything else in `SECRET_PATHS` are plainly readable.

So the harness is **stricter with Marlow's own shell calls than with the scripts
it schedules**, which is backwards. A `wc -l ~/.ssh/id_ed25519` typed by Marlow
into `Bash` is hard-denied *and* would find an empty namespace; the same line
inside a promoted script runs unimpeded every morning.

## Why it is deferred rather than urgent

- The promoted list is short and each entry was installed by hand by Mike.
- Least privilege inside each script is the interim mitigation (the first
  promoted script, the Trello sync, holds a read-only token).
- Promotion requires `sudo`, so the set cannot grow without Mike.
- OpenShell / NemoClaw is the intended real jail. Anything built here is
  scaffolding with a known replacement, which argues for the smallest possible
  version.

## Shape, if built

**Reuse, don't reinvent.** `invoke.py` already constructs the namespace and is
the only place that knows how; the work is extracting that construction so a
second caller can use it, not writing a second one. A drifting copy of the
confinement arguments is the failure mode to design against — the same lesson
`resolve-paths-once.md` catalogues for path resolution.

Sketch: a `bwrap` wrapper invoked by the crontab line, so the form becomes
`<schedule> /opt/marlow/bin/confine.sh <name>` rather than the script directly.
Root-owned like everything else under `/opt/marlow/bin/`.

Two things to settle before writing any of it:

1. **A cron script's needs are not `claude -p`'s.** The Trello sync makes an
   outbound HTTPS call and writes into the working tree; the CLI's namespace is
   shaped around a different job. Whether one profile serves both, or the
   wrapper takes a per-script profile, is the actual design question.
2. **Failure mode.** `MARLOW_BWRAP_ENABLED` is a flag, and a box without the
   AppArmor profile opts out. Decide whether a confinement failure means the
   script does not run (loud, and a morning briefing silently loses a section)
   or runs unconfined (quiet, and the property is not a property). Prefer loud —
   the v1 failure this whole design exists to prevent was a scan that quietly
   stopped firing.

## Decision rule — when this stops being deferred

Build it when the promoted list reaches roughly **four scripts**, or when any
promoted script needs a credential that is *not* read-only — whichever comes
first. Either one means the "short list of least-privilege scripts" mitigation
has stopped being the real control.

## The other half — scheduling a promoted script, parked 2026-08-30

Separate concern from confinement, recorded here because it is the same
promoted-script surface and the same trade.

`docs/tasks/schedules-and-reminders.md` gives the crontab a single writer: the
renderer owns the whole file and emits only `entrypoint.py` lines, so a promoted
script can no longer have a crontab line at all. It gets a root-owned systemd
timer instead (`deploy/marlow-trello-sync.timer` is the first). **Marlow lost
nothing** — it could never install a crontab line, so self-scheduling a script
was never available. What changed is Mike's cost: a promoted script now takes a
unit file rather than one crontab line.

The want, in Mike's words (2026-08-30): *"it's much more useful to ask Marlow
'write this script for yourself to do X, run it weekly' than to ask for the
script and then promote it to systemd myself."*

Most of that want is already met by a `prompt` entry — recurring model work
needs no approval at all. The genuinely uncovered case is **deterministic,
cheap, non-model recurring work**: the Trello sync is curl and jq, no tokens, no
~58K identity re-injection, identical every run. That is a real category and a
narrow one.

**Shape, if built:** a `script` payload kind whose gate is the same shape as
`job`'s. The store names `scripts/<name>.sh`; the renderer emits
`/opt/marlow/bin/<name>.sh`; **the existence of a promoted copy is the
allowlist**, exactly as an approved `cron/<job>.md` is for `job`. Marlow
schedules and re-times freely, the content still costs a promotion. No model
text reaches a command line — a harness-owned constant path plus an allowlisted
name — and the confinement gap above is unchanged, neither widened nor closed.

**Decision rule:** build it when Mike promotes a **second** script that needs a
schedule. One promoted script is a systemd unit; two is a pattern, and at that
point he is hand-writing his second unit file for something the renderer should
be emitting.

## Reference

`deploy/README.md` §6b (the promotion convention and the asymmetry it records),
`docs/tasks/scheduled-work.md` → "Amended 2026-08-08", `server/invoke.py` (the
namespace construction to extract), `server/bash_policy.py` module docstring
(why the boundary moved into the kernel), `scripts/probe-bwrap-confinement.sh`,
`docs/tasks/schedules-and-reminders.md` (the renderer that owns the crontab).
