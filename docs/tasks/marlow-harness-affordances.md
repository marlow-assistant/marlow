# Task — tell Marlow what affordances it actually has

**Status: CLOSED 2026-09-06, and closed by disproof rather than by shipping.**
Piece 3 (richer denial reasons + a denial log) shipped and is live.
Piece 1 (an always-on generated `# HARNESS` block) is **abandoned** — the
measurement below says wording is not the lever. Piece 2 (a skill) stays parked
behind blockers that never cleared. The problem this doc was opened about is
real and is now `docs/tasks/tool-approval-ux.md`'s, approached from the approval
side instead.

Read this before reopening any "tell the model about the harness" idea. It is
kept for the falsification, not for the design.

## What it was for

Marlow runs inside a harness with knowable rules and no description of them, so
it discovers them by being refused. The worked example, a `physio-nudge` cron
run denied in full on 2026-08-08:

```
tail -n 20 .../logs/fitness-log.md 2>/dev/null; echo "---VAN---"; cat .../logs/van-board.md 2>/dev/null
```

Four vetting failures at once (`tail`, `echo`, `cat` all off `VETTED_COMMANDS`;
`;` off `VETTED_TOKEN_CHARS`; the redirects abandoning vetting again) — and both
files sit inside the working tree, where `Read` would have been auto-allowed
twice with no prompt. **Marlow's errors are not attempts to exceed its
permissions; they are detours around capabilities it already has.** That framing
still holds and is the reason the problem is worth solving. Only the proposed
fix was wrong.

**That exact command is auto-approved as of 2026-09-06** — content-verb vetting
plus the shape allowances, `tool-approval-ux.md`. Verified on the box against a
version with both files present; the original prompts today only because
`logs/fitness-log.md` no longer exists, which is the new rule's regular-file
clause doing its job rather than a vetting failure. The cost came off the shell,
which is what this doc concluded had to happen once coaching was disproved.

## Why piece 1 is abandoned — the doc's own test, failed

This doc set the condition itself: coach the failing job prompts, and *"if the
rate does not drop, the problem is not wording and this doc is wrong about its
own cause."*

**Acted on 2026-09-01** (`~/marlow-identity`): `physio-nudge` and
`alberta-weekly` were rewritten to name `Read`/`Glob`, and `morning` got the
standalone `ls -l` line. `physio-nudge.md` names the forbidden command outright
— *"use the `Read` tool… the tool, not a shell command. `cat` is denied on this
run and nobody is awake to approve it."*

**It ran `cat` anyway. Sep 4 and Sep 5, in that job, from that prompt.** That is
coaching at maximum strength — the right tool named, the wrong tool named, the
consequence explained — failing in the same file a week later. The van-board and
alberta-weekly halves *did* clear, because those were a capability gap (nothing
vetted returns an mtime; `ls -l` does) and a missing tool name, not a routing
preference. What survived the coaching is exactly the part coaching cannot
reach.

## The mechanism, measured 2026-09-06

Across Marlow's CLI transcripts:

| tool | calls |
| --- | --- |
| `Read` | 294 |
| `Bash` | 160 |
| `Grep` | **0** |
| `Glob` | **0** |

Bash verbs: `ls` 36, `cat` 26, `grep` 24, `git` 21, `find` 9. **104 of the 160
Bash calls (65%) carry a pipe, redirect or `&&`**, so vetting is abandoned
before the verb is read — even a vetted `ls` prompts most of the time.

**`Grep` and `Glob` were not in the CLI's tool list AT ALL** — corrected
2026-09-06, and the earlier version of this section had the mechanism wrong.
It said they were "deferred behind `ToolSearch` while `Read` is eager", and
treated Marlow never typing `select:Grep` across 106 `ToolSearch` calls as
evidence of a routing preference. It was not a preference. **The tools did not
exist**, and `select:Grep` would have failed had it ever been attempted.

Read from the CLI's own `system/init` event rather than by asking the model
(`--output-format stream-json --verbose`, first line, the `tools` array). That
distinction matters and cost a wrong answer on the way here: asked directly in
the same session, the model reported Trello as available while the init event
showed it was not. **Probe the init event; do not interview the model about its
own tool list.**

Baseline at CLI 2.1.251 — 54 tools, no `Grep`, no `Glob` anywhere in it:

```
Bash Edit ListMcpResourcesTool Monitor PushNotification Read
ReadMcpResourceDirTool ReadMcpResourceTool ReportFindings ScheduleWakeup
SendMessage Skill ToolSearch WebFetch WebSearch Write   (+ 38 connector tools)
```

**A flag does change it, and this doc previously said none did.** `--tools`
("Specify the list of available tools from the built-in set") names the
built-ins explicitly, and with `Grep`/`Glob` in that list they are present and
eagerly offered. Shipped in `server/invoke.py` as
`boundaries.VETTED_BUILTIN_TOOLS`; end-to-end on the box, a turn asked to
`Grep` under `memory/` and `Glob` `memory/*.md` ran both with
`permission_denials: NONE` — `file_policy` already vets them by path, so no
new permission work was needed.

It is not `--strict-mcp-config` in disguise, which is the first thing to check
about anything that trims this surface: `--tools` governs BUILT-INS only, Gmail
(29) and Calendar (9) survive it, and settings.json's denies still apply on top
(Trello and Drive stay gone). Verified in the same probe.

The old eager-set argument below is kept because its *reasoning* still holds —
it is simply answering a different question than the one that mattered.

The eager set without the flag was:

```
Bash  Edit  Read  Write  ReportFindings  ScheduleWakeup  Skill  ToolSearch
```

Against the 2026-08-23 probe (`marlow-settings-permissions-policy.md`) that is
the same set minus `Agent` and `ListAgents` — the tool-surface trimming deny working exactly as
designed. **Denying two eagerly-loaded tools freed two eager slots and nothing
was promoted to fill them.** If eager-vs-deferred were a budget, that is
precisely when a promotion would have happened. It is a fixed list — which was
true, and which is why no amount of *trimming* would ever have surfaced `Grep`.
Naming it outright is what worked.

So the conclusion is not "Marlow prefers the CLI." It is that **Bash `grep` was
Marlow's search tool because there was no other one**, and the model cannot be
coached toward a tool it has not been given. Both halves of this doc's original
claim were about the wrong layer: coaching failed (correctly diagnosed) and the
harness was missing a capability (diagnosed as a preference).

## What follows, and where it went

If Marlow cannot be routed away from the shell, the cost has to come off the
shell instead — which makes this an approval-ergonomics problem, not a
description problem. **`docs/tasks/tool-approval-ux.md` owns it, and shipped it
2026-09-06:** 48 of the 143 Bash prompts (33.6%) are gone.

Note what did NOT get built there, because this doc's disproof is the reason.
The plan of record was a turn-scoped "yes to similar" grant. Replaying it
against the corpus scored 13%, because most prompts are the *first* use of a
verb in a session — Marlow's Bash traffic is heterogeneous and has no repetition
to amortise over. Static vetting was the cheaper and larger answer.

There is one live consequence for this doc's own conclusion. Coaching failed at
routing Marlow *away* from the shell, but the harness now has to tell it which
shell commands are free, or it will keep paying for detours around a capability
it has — the schedules-and-reminders failure again. `identity/CLAUDE.md` grew a "Shell commands"
section for exactly that, and it is a description of what is cheap, not an
instruction about which tool to prefer. That distinction is the surviving lesson
here: describing an affordance works, arguing with a trained reflex does not.

Mike's named next step past that (2026-09-06): re-challenge the assumption that
CLI paths can be vetted at all, or move toward the kind of standing
auto-approval Claude Code itself now applies. Both are larger and scarier, and
neither should start before the shipped rule has been lived with.

## Piece 2 (a skill) — still parked, blockers unchanged

Mike's reframing (2026-09-01) is the good version: not a skill loaded while a
job *runs*, but one loaded when **authoring** a job — "make a cron for yourself
to do X" → "I should read the skill first." That is an attended iOS turn, so
`Skill` draws a prompt rather than cron's auto-deny.

Three constraints if it is ever unparked:

- **`Skill` is absent from `boundaries.VETTED_PATHLESS_TOOLS`**, so it
  auto-denies in cron. The fix is one entry there plus the bare-name entry in
  `deploy/marlow-settings.json`; `tests/test_marlow_settings.py` already asserts
  those two move together. Copy `ToolSearch`'s rationale — it grants *loading*
  instructions, not use, and anything the skill then says to do still faces the
  hook individually.
- **The detail cannot live in a file the model `Read`s.** A project skill under
  `<working_dir>/.claude/` is inside `AGENT_CONFIG_PATHS`, denied for reads as
  well as writes by design; a user skill under `~/.claude/skills/` is outside the
  working tree, so containment prompts it. Only what the CLI loads *itself* on
  invocation works. So: one self-contained `SKILL.md`, no bundled-file
  indirection. And if it lands in `~/.claude/skills/`, add that to
  `AGENT_CONFIG_PATHS` — a file telling Marlow what it may do is exactly what
  Marlow must not be able to rewrite.
- **Nobody has confirmed `claude -p` surfaces skills at all**, or from which
  directory. Do not test by nesting `claude -p` in a builder session; hand Mike
  a clean-shell script.

Writing a new `cron/<job>.md` is also a two-approval flow — `file_policy`'s
writable set is `memory/` plus the named `cron/schedules.json`, so creating the
prompt file prompts the phone. Attended that is desirable, but a skill that did
not say so would teach an incomplete story.

## Piece 3 — SHIPPED

Denial reason strings rewritten as named constants in `hooks/pretooluse.py`, and
all five denial classes logged to `~/.marlow/logs/marlow-denials.log`, tagged
`denied/<kind>`. **`server/deny_detail.py` carries the table of what each kind
means and what a repeat of it indicts** — read it there, not from a restatement
here.

Two rules from that work that outlive it:

- **The standard for any new deny reason** (from the lesson of the permission
  round-trip UI work, cited in `hooks/pretooluse.py`): no module names — jargon in that position reads to the
  model as spoofed tool output. Say plainly that a thing is never allowed rather
  than implying a retry lands differently, and give the why.
  `tests/test_pretooluse.py` asserts `boundaries.py` never appears in a reason.
- **The denial log does not rotate, deliberately** — rotation is what makes
  concurrent writers unsafe, and the hook is a fresh process per tool call.
  If it ever grows enough to matter, that is the signal arriving loudly, not a
  logging problem. External rotation (logrotate + `WatchedFileHandler`) if it
  still needs bounding; never a `RotatingFileHandler`.

Treat that file as secret material: a denied command string is arbitrary model
output and `RedactingFilter` is field-targeted, so a secret in some other shape
is written verbatim. Accepted (anything the model could put in a command it
could already read) but it means the file does not leave the box casually.

## Reference

`docs/tasks/tool-approval-ux.md` (where the live problem went) ·
`docs/tasks/denied-tool-workarounds.md` (the decision rule for allow-vs-coach,
and how to re-read the denial corpus) ·
`docs/tasks/marlow-settings-permissions-policy.md` §"tools are deferred" (the
eager-set probes) · `server/deny_detail.py` (denial classes) ·
`hooks/pretooluse.py` (reason strings, hard denies).
