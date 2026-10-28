# Denied tool calls, and what Marlow does about them

**Status: CLOSED 2026-09-01.** This was a standing "go read the denials and
decide" task. It has been read twice — 2026-08-12 and 2026-09-01 — and both
readings are now settled. The second lives in
`docs/tasks/marlow-harness-affordances.md` §"The re-read", which is also where
the remaining work is. What survives here is the part that outlives either
reading: the **decision rule**, the **recovery behaviour**, and **how to do the
read again** if someone needs to.

## The decision rule — still the answer

Every avoidable denial is one of exactly two things, with opposite fixes: the
tool was safe and should be vetted, or the tool was reasonably refused and
Marlow should not have reached for it. The rule for telling them apart:

> **Prefer coaching over allowing when a vetted equivalent already exists.**

Allow only when there is **no** vetted equivalent, the call is genuinely
contained, and it recurs often enough to be worth a standing grant. Two
constraints behind that, both of which have already bitten:

- **`server/bash_policy.py` got caught twice by enumerating the dangerous
  cases** — a verb denylist let `git diff --no-index ~/.ssh/id_ed25519` read any
  file, and a metacharacter denylist let brace expansion past containment. Both
  fixes were to enumerate the *safe* set instead. Assume anything denylist-shaped
  here has a third hole.
- **A vetted-allow is a standing grant and it applies unattended** — exercised at
  08:00 with nobody watching, against inbox text written by strangers.

`cat`/`tail`/`grep` are the clearest case: `Read` and `Grep` already do the job,
are already vetted tree-wide, and are *better* — a `limit`, structured content,
no shell parsing. Adding them would buy one round trip and put a
contents-printing verb on the Bash path, which is the shape that produced the
`git diff` hole. `boundaries.VETTED_COMMANDS`' own comment records `grep` being
added and removed the same day; do not re-litigate it without reading that first.

**Where coaching goes was the open question, and it is answered: both.** Per-job
coaching works and reaches exactly one job prompt — the 2026-09-01 read found the
two prompts that named the tools mostly clean and the two that did not failing
every run. That is three files needing the same sentence, which is the threshold
this doc set for promoting it into the always-on injection (piece 1 of the
affordances doc). Per-job stays too; it is free and it is where job-specific
detail belongs.

## The recovery behaviour is the good one — do not "fix" it

The reason this task's priority kept going *down*. Given a denial, Marlow
degrades and says so. From the 2026-08-09 digest, unprompted:

> *"Skipped: full memory file reads were permission-gated in this unattended
> run, so this digest ran off the index summaries already in context rather than
> the source files…"*

Accurate, specific, volunteered. The failure mode is **degraded-but-honest, not
silently-wrong**, and the `emit()` wording in `hooks/pretooluse.py` ("Carry on
without this tool and say what you skipped") is what produces it. Know that
before editing that string.

The real cost is not the denial, it is the **retry loop** around it. The worst
observed turn spent 9 tool calls landing one read: it fixes the tool while
reverting to the bad path, then fixes the path while back on Bash. ~50s and 8
refusal round-trips. **The failure mode is flailing, not giving up** — which is
why a better description beats a better denial string.

## How to do this read again

- `~/.marlow/logs/marlow-denials.log` — every denial, tagged `denied/<kind>`.
  The kinds are not interchangeable evidence; `server/deny_detail.py` has the
  table of what a repeat of each one indicts.
- `usage.jsonl` gives the *rate* (`had_permission_denial`) and the denominator by
  source. **Cross-read the two**: a denial with no invocation in telemetry at
  that timestamp is not Marlow at all, it is a `claude -p` run by hand over ssh,
  and it denies by design. Three of the 25 lines read on 2026-09-01 were that.
- **`usage.jsonl`'s `session_id` IS the transcript filename**
  (`~/.claude/projects/-home-marlow-marlow-identity/<session_id>.jsonl`), which
  is what makes an after-the-fact read cheap. Recorded nowhere else.
  `marlow-last-turn.sh` (in `mike`'s home on the box) does the most recent turn.
- The server persists no turn results and logs no content; the CLI transcript has
  everything. So "what did Marlow actually say" is always answerable, just never
  from our logs.

**Do not build a dashboard for this.** A periodic read with the tools above is
the right weight.
