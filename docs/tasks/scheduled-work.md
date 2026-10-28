# Task — scheduled (cron) work: `cron/entrypoint.py`

**Status: BUILT and MERGED 2026-07-29** — real cron (P1), then a follow-up for the import
path and an `internal_app` startup race. `cron/entrypoint.py` exists,
`tests/test_cron_entrypoint.py` covers it. Corrected 2026-08-01; this line said
"not started" long after it shipped. What remains is **operational, not code**:
installing `entrypoint.py` root-owned on the deploy box and authoring the job
prompt files under `$MARLOW_WORKING_DIR/cron/*.md` (personal content — belongs
in the identity repo, never here). The rest of this doc is the spec it was built
from, kept for the reasoning. **Priority (historical):** FIRST of the three "proactive Marlow"
tasks — see `docs/tasks/apns-push-delivery.md` and
`docs/tasks/ios-minimal-client.md`. **Shape:** one small script, one internal
endpoint, a crontab convention, and a boundaries decision. Deliberately little
code.

## Two defects found by running it, 2026-09-06

Both surfaced on the same store, on a day nobody was working on cron.

**A refused store was silent.** `sync_after_turn` treats a `None` from `load()`
as "nothing to install" and returns — right for a box with no schedules, wrong
for a box whose store is broken. Marlow wrote one bad entry; the store was
refused on every turn for half an hour, the crontab stayed frozen at its last
good version, and the only trace was an ERROR in the journal. The write
succeeded and the file reads back exactly as written, so the model had no way to
observe it. `schedules.rejection_reason()` now feeds `turn_context`, which says
so in the per-turn block and names the entry.

The general shape is worth keeping: **the schedule store is the one piece of
harness state Marlow both owns and cannot observe the effect of.** Anything else
that acquires that property needs the same treatment.

**`at`+`job` was refused, and should not have been.** A one-off that runs a job
prompt file is an obvious thing to want, and Marlow reached for it three times.
The refusal was a proxy for a real constraint — `render()` emitted the direct
`entrypoint.py <job-name>` form for any job, pruning lives on the `--entry`
path, and a cron line carries no year, so a one-off job would have fired again
the following year. Rendering one-offs through `--entry` fixes the actual
problem; `_run_entry` grew the job branch that its own `else` clause said could
never be reached.

Told only that the pairing was illegal, Marlow's workaround was
`cron: "0 9 9 9 *"` plus a note to itself to delete the entry after it fired —
which validates, and is the same yearless line with a prune no turn-scoped agent
performs. **A validation rule that stands in for a rendering bug teaches a worse
shape than the one it forbids.**

## The design decision — settled 2026-07-29: use real cron

**Use the system crontab, under Marlow's own unix user. Do not build a
scheduler.**

Two richer designs were considered and rejected, in this order:

1. **A templated systemd timer per job.** Robust, but it splits a "job" across
   two places that don't know about each other — the prompt in the identity
   repo, the schedule in unit files on the box — and adding a job means SSH plus
   a unit file plus `systemctl enable`.
2. **One timer ticking a dispatcher that reads a declarative schedule file.**
   Proposed to make the schedule *data* Marlow could read and reason about. Both
   of its claimed advantages evaporated on inspection:
   - **Introspection is already free.** "Marlow can see its own schedule" is
     `crontab -l`. No file format, no parser, no tick loop.
   - **Skip-on-downtime is the behaviour we want, and cron does it by
     default.** The dispatcher design needed a max-lateness window so that a
     07:00 scan wouldn't fire at 14:00 after an outage — which is precisely
     cron's default. It was code to reproduce a default.

   What remained was a tick loop, due-time calculation, last-run state, and
   catch-up bounds: new code with new bugs, replacing code that has been
   debugged since the 1970s.

Sonnet already writes and manages crontabs competently — it did so for all of
v1's scheduled jobs. The wrapper below is needed under any of these designs; the
scheduler was the only difference, and Unix already ships one.

## Security posture — where the real question was

The interesting risk is not scheduling, it's that a crontab is an execution
surface *outside* the harness. A crontab line runs as the marlow user with no
PreToolUse hook, no `boundaries.py`, and no work queue — a clean bypass of
everything gates #2 and #4 exist to build. Three cheap properties close it,
none of which needs new machinery:

1. **Crontab entries only ever invoke `cron/entrypoint.py <job-name>`.** There is
   no per-job shell script in this design, so there is no `foo.sh` for Marlow to
   quietly rewrite into an escape.
2. **`entrypoint.py` is installed root-owned and not writable by the marlow
   user** (`install -o root -m 755`). Marlow executes it, never edits it. This is
   file ownership on a file the deploy already installs, not a policy layer.
3. **Crontab reads prompt and crontab *writes* prompt** (`boundaries.py`), so a
   schedule change becomes a permission prompt on Mike's phone. Corrected
   2026-08-08: this used to say `crontab -l` was "allowed outright", which was
   never true — `crontab` is not in `VETTED_COMMANDS`, so the read prompts too and
   an unattended turn auto-denies it. The asymmetry the design wanted (read
   freely, write with a human) does not exist today; what a job can read without
   a prompt is its own `cron/*.md` prompt file, via the `Read`/`Glob` tools.

   The *write* half is still exactly the human-*intent* call that plan §4 says to
   reserve the tiny ask-set for — not a blast-radius rule, which would belong in
   §5. Marlow can reason about its own schedule and propose changes; applying one
   needs a human. Adding `crontab` to `VETTED_COMMANDS` for reads is not a
   one-liner and should not be done casually: vetting is per-command-name, so it
   would clear `crontab <file>` and `crontab -r` too unless the arg shapes are
   handled — which is the denylist shape this repo keeps getting caught by.

### Amended 2026-08-08 — property 1 has a bounded exception: promoted scripts

Property 1 above ("there is no `foo.sh`") held for eight days. The first real
crontab install put one in: a Trello sync living at
`~marlow/claude-assistant/scripts/trello-sync.sh`, inside Marlow's own working
tree, running daily as `marlow` outside the harness.

**Mike's call, made with the tradeoff stated: Marlow authoring and running
scripts is worth keeping — it is most of what makes it useful — so the answer is
not to forbid scripts but to stop fusing two separate privileges.** Authoring is
cheap and reversible (a file sits there; nothing has run). Scheduling is the
expensive one. `deploy/README.md` §6b is the mechanical convention; the rule is
that a crontab line may invoke `entrypoint.py` or a **root-owned promoted copy
under `/opt/marlow/bin/`**, and nothing inside the working tree, ever.

Why promotion rather than relying on the write prompt: `file_policy` auto-allows
writes only under `memory/`, so an in-tree script edit *does* raise a phone
prompt today. But that prompt reads "Write `scripts/foo.sh`" — indistinguishable
from an ordinary file edit, while actually granting permanent unattended daily
execution. Reviewing at the cheap step, on every edit forever, with no signal
marking the one that matters, is not a control that survives contact. `sudo
install` is a step Mike takes deliberately and rarely, and it is where the diff
gets read.

Two things this does **not** fix, both recorded rather than papered over:

- **A promoted script runs outside bwrap** and sees the secret paths that
  `claude -p` cannot. The harness is stricter with Marlow's own shell calls than
  with the scripts it schedules. → `cron-script-confinement.md`.
- **Marlow does not know any of this.** It has no description of the affordances
  it gets under this harness, so it cannot author a script that fits the
  convention or explain to Mike why one was refused. → `marlow-harness-affordances.md`.

The interim mitigation for both is least privilege inside each script (the
Trello token is read-only) and a deliberately short promoted list. OpenShell /
NemoClaw is the real jail; do not build a bespoke containment layer here ahead
of it.

**Prompt files stay writable by Marlow, and that is fine.** A prompt file is fed
back through the harness, so it becomes a *prompt*, not an execution — whatever
the resulting turn attempts still passes the hook and `boundaries.py`. It is a
self-influence channel, not an escape, and it is the same category as Marlow
writing its own memory, which this design already accepts by construction.

Beyond that, rely on ordinary Unix permissions and the service's dedicated
restricted user (plan §5.1). The general case — an agent that can modify things
that later execute — is what NemoClaw/OpenShell is for; `boundaries.py` is
already shaped to generate that policy rather than drift from it. Do not build a
bespoke containment layer here.

## Where things live

**Job prompts live in the identity directory, NOT in this repo.** This is the
hard content rule (root `CLAUDE.md`,
`docs/marlow-v2-identity-architecture.md`): no personal content ever enters
`marlow-server`'s git, and "scan my email and tell me what needs a reply" is
personal content — it names Mike's habits and priorities.

- `cron/entrypoint.py` (this repo) is the **mechanism**.
- `$MARLOW_WORKING_DIR/cron/<job>.md` (identity dir, private repo) is the
  **content**.
- `$MARLOW_WORKING_DIR/cron/schedules.json` (identity dir) is the **schedule**,
  owned and edited by Marlow; the server renders the crontab from it inside the
  same turn (the schedules-and-reminders fixes of 2026-08-31). The crontab is an artifact, not the source, and
  `crontab` itself is denied — so the "introspection is free, it's `crontab -l`"
  argument above is history, not current practice.

## As built

Implementation, test plan, and landing steps have been retired — this shipped 2026-07-29. The code is the spec now: `POST /internal/cron/message` in `server/app.py` (internal_app only — a test asserts it absent from public_app), `cron/entrypoint.py` (stdlib + `server.config`/`server.log`, deliberately no venv dependency), the crontab convention in `deploy/README.md` §6a, and `tests/test_cron_entrypoint.py`.


### Scheduling constraint — carry this into whatever schedule gets set

Each cron invocation re-injects the ~58K-token identity block, and the prompt
cache does not dependably survive gaps beyond **240 seconds**
(`docs/marlow-v2-gate1-results.md` §8b). v1's habit of spreading morning tasks an
hour apart was close to worst-case: it paid a full cold write every time and
bought nothing Mike could perceive. Prefer **fewer, coarser jobs** — or several
inside one short window, spaced under 240s. Do not plan a schedule around a
larger number without measuring it first.

## Known gap — cron has no separate ALLOW policy

Plan §4 specifies that unattended work runs a strict never-prompt allow-list and
never routes a question anywhere ("blocking a 2am job on a sleeping human is
never correct"). **Half of that exists now, and it is the half that was
dangerous.** `server/active_turn.py` tells the permission endpoint whether a
human is behind the invocation; an unattended turn is denied **instantly**, with
`denied/unattended` in the denial log and a reason string that tells the model
nobody was asked. It no longer blocks for `t_hook_seconds` and no longer posts
"I couldn't reach you" into a chat nobody is reading. Anything in an older doc
describing the 55-second cron timeout is stale.

What is still missing is a distinct **allow** set: a cron turn is vetted against
the same lists as an iOS turn, so a job that needs an approval simply fails,
every night, at the same time. That is the `denied/unattended` class, and it
indicts the **job prompt** — see `docs/tasks/marlow-harness-affordances.md`,
where the 2026-09-01 read found 30% of cron turns hitting one, nearly all of them
detours around tools the job already had.

So: **write job prompts to stay inside what is already allowed, and name the
tools explicitly** (`Read`, `Glob`, `Grep` — not `cat`, `ls | tail`, `grep`). Do
not paper over a recurring denial by widening the global allow set; that weakens
the interactive path too.

## Reference

Plan §2 (tree layout), §4 (unattended never-prompt, and the ask-set discipline),
§5.1 (argument-shape gating), §7 step 5 (cron cutover), gate1-results §8b (the
240s window). Delivery of cron results to Mike's phone is
`docs/tasks/apns-push-delivery.md`. The nightly session-rollover job that will
eventually be scheduled this way is `docs/tasks/nightly-consolidation.md`.
