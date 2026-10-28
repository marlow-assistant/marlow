# A probe suite / `doctor` mode

**Status:** idea, captured 2026-08-06 (Mike's). Not scheduled. Do not build this
before there is a second occasion to run it — see "When to build" below.

## The observation

`scripts/` has accumulated one-off probes, each written to answer a single
question at a single moment:

| script | question it answered |
|---|---|
| `probe-allow-vs-hook.sh` | does the PreToolUse hook fire for allow-matched tools, and does a hook deny bind over `permissions.allow`? |
| `probe-bwrap-confinement.sh` | can bwrap hide the secret paths from `claude -p` on this kernel? |
| (next) connector probe | do Claude.ai connectors survive `--strict-mcp-config`? |

Each was written as disposable and each turned out **not** to be. The hook probe
was re-run on two machines and two CLI versions; the bwrap probe was re-run
three times as the AppArmor situation changed, and once more after a reboot.
Every one of them encodes an assumption the deployed system rests on, and every
one of those assumptions belongs to software we do not control: the `claude`
CLI, the kernel's userns policy, the account's connector set.

The pattern: **these are coarse acceptance tests for our dependencies.** They
answer "did the behaviour we built on change?" — exactly the question you have
when something is broken and nothing in our own git history explains it.

## The shape it wants

`marlow doctor` (or `scripts/doctor.sh`) — run every probe, print a table of
CONFIRMED / INCONCLUSIVE / CHANGED, exit non-zero on any change from the
recorded baseline. Two clear triggers:

- **A `claude` CLI upgrade.** The deployed unit sets `DISABLE_AUTOUPDATER=1`, so
  version changes are deliberate events arriving via `update.sh` (START's Known
  gap #1 wants a CLI-contract smoke test for exactly this reason — that gap and
  this idea are probably the same piece of work, and should be built as one).
- **Anything inexplicable.** The value is having a cheap first move that is not
  "read the CLI changelog and guess".

Design notes, from what the existing probes already had to solve:

- Probes must run in a **clean shell**, never nested inside a Claude Code
  session — a child inherits the harness env and reports false results
  (CLAUDE.md). So this is a script Mike runs, or a systemd timer, and **not**
  something the server or a builder session can invoke itself. That constraint
  is the main design pressure and it rules out the obvious "expose it on
  `/health`" answer.
- Every probe needs **its own control**, or a failure is indistinguishable from
  a broken probe. Both existing scripts learned this the hard way — the bwrap
  probe's first "answer" was a control that failed for an unrelated reason
  (the CLI refuses `~/.ssh` on its own account, above any rule of ours).
- Probes cost real subscription usage. A suite that is expensive to run is a
  suite nobody runs.

## When to build

**Not yet, and not on principle.** Two probes plus one pending is not enough
shared structure to factor, and guessing at the common shape now would be
speculative machinery. The signal to build is the *third* time someone re-runs
a probe to answer a question it was not written for — at that point the
generalisation is observed rather than predicted.

Until then the cost of not having it is low: the scripts exist, they are in git,
and each has its own verdict logic. What is genuinely missing today is a
**recorded baseline** — what each probe returned, on which CLI version and which
kernel. That part is worth doing opportunistically as each probe is run,
inside the relevant task doc, and it is most of the value of a suite anyway.
