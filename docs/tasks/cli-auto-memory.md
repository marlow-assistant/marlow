# Task — the CLI's own memory feature competes with Marlow's

**Status:** ✅ **shipped 2026-08-11** — merged, deployed, and confirmed
live. `server/invoke.py` sets `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` on every
`claude -p` child. Deploy verified by file hash rather than by assumption (the
box's `invoke.py` is byte-identical to `main`, and the service restart follows
the commit by 34s), and Mike confirmed from the phone that Marlow now reports
its actual identity content with no trace of the CLI's directive. The orphaned
empty `memory/` dir the feature had created on the box was removed the same day.

This file is the why, because the symptom was hard to diagnose and the fix
looks like an arbitrary env var.

## Symptom

Marlow repeatedly went after
`/home/marlow/.claude/projects/-home-marlow-marlow-identity/` — the memory
directory under it, and the raw `.jsonl` session transcripts beside it — to
answer questions about what it knew or what was said earlier. Asked why, it
said the memory path came from its system instructions and the transcript path
was its own inference about CLI storage layout. Both halves were true.

That path had previously been assumed hallucinated. It was not.

## Cause

The Claude Code CLI has a built-in memory feature, independent of anything this
repo does. When on, it prepends to the system prompt:

> You have a persistent, file-based memory system at `<path>`.

…where `<path>` is `~/.claude/projects/<encoded-cwd>/memory/`. With the working
dir at `/home/marlow/marlow-identity` that renders the directory above, which
the CLI had already created on the box (empty, 2026-08-10).

Nothing on disk contains that sentence — it is assembled at runtime inside the
CLI binary, which is why grepping the repo and the box's config found nothing.
`server/identity.py` is not involved and never emits an absolute path: under
`index_only` it names `memory/`, relative to the CLI's cwd.

## Why it had to be turned off rather than lived with

It left Marlow believing in two memories:

| | injected by us | the CLI's |
|---|---|---|
| path | `/home/marlow/marlow-identity/memory/` | `~/.claude/projects/-home-marlow-marlow-identity/memory/` |
| source | `server/identity.py`, via `--append-system-prompt` | CLI system prompt |
| writable | yes — `Edit(.../memory/**)` | **no** |

The CLI's directive also tells the model to *write* memories there. Every such
write is denied: `Write(/home/marlow/.claude/**)` in
`deploy/marlow-settings.json`, with `boundaries.AGENT_CONFIG_PATHS` behind it.
Loosening those to make the write succeed is not on the table — that subtree is
where the credentials file and the agent config live.

So the choice was between an instruction the model is given and structurally
cannot obey, and no instruction. The second is better: a standing impossible
directive spends attention every turn and, as observed, points that attention
at the transcript directory.

## The fix, and the one trap in it

`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, checked by the CLI itself before it
composes that prompt.

**It cannot go in `deploy/marlow-server.service`.** `invoke._SCRUB_PREFIXES`
strips every `CLAUDE_CODE_*` from the child env, so an `Environment=` line
would be silently dropped — the setting would look configured and do nothing.
It is set *after* the scrub instead, via `invoke._FORCED_ENV`, which is a
deliberate exception: the scrub exists to drop CLAUDE_CODE_* state *inherited*
from a parent Claude Code session, and this value is ours. Two tests in
`tests/test_invoke.py` pin both halves — that it survives the scrub, and that
an inherited `=0` cannot re-enable the feature.

## Proof that the var works (2026-08-11)

The claim above started as a string grep of the CLI binary. It was then
confirmed empirically on the box, in a clean login shell over ssh (verified: 0
`CLAUDE*`/`AI_AGENT` vars inherited — nesting `claude -p` inside a Claude Code
session would have inherited the parent's env and proved nothing). CLI 2.1.223,
the build the box runs. Script: `scripts/probe-auto-memory.sh`.

Two arms, each in a *fresh* `mktemp -d` cwd so the filesystem signal is
unambiguous, and so Marlow's real project dir is untouched:

| | baseline (var unset) | `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` |
|---|---|---|
| `~/.claude/projects/<encoded-cwd>/memory/` created | **yes** | **no** |
| model self-report | quoted the directive verbatim | `NO-MEMORY-DIRECTIVE` |

The baseline arm quoted:

> You have a persistent, file-based memory system at
> `/home/marlow/.claude/projects/-tmp-probe-am-baseline-GRefgs/memory/`.

Note the path: the scratch cwd, not Marlow's. That is the direct evidence the
path is derived from cwd at runtime, which is why it existed in no file to
grep.

The filesystem row is the load-bearing one — it does not depend on the model
reporting its own prompt honestly. The self-report row corroborates; its prompt
offered an explicit `NO-MEMORY-DIRECTIVE` escape so that agreeing was not the
path of least resistance.

Two things to know before re-running it: the invocations bill against the Pro
subscription like any other, and they run **outside the server's work queue**,
which is what normally serializes all `claude -p` calls. Check that no `claude`
process is in flight first (`pgrep -af "[c]laude"` — the bracket matters, a
plain `pgrep -f` pattern matches the checking shell itself and reports a
phantom).

## What this does not cover

The transcript half was Marlow's own inference, not a configured path, so
disabling the feature does not by itself stop a future reach toward the
`.jsonl` files.

**Be precise about what stopped it, because it was not a policy.**
`~/.claude/projects/**` is in neither `SECRET_PATHS` nor `AGENT_CONFIG_PATHS`,
and `deploy/marlow-settings.json` denies `Read` only on `.credentials.json` and
`~/.ssh/**`. The transcript read therefore fell through to the PreToolUse hook
and became a phone prompt — which Mike happened to deny, while attended. Tapped
Allow, or unattended with a different default, and it would have read the file.
So "it was denied" is true of that one attempt, not of the mechanism.

Whether that matters is a real question and deliberately left open: those files
are the full text of every conversation, and Marlow reading its own past
transcripts is not obviously wrong — it is arguably what a
persistent-memory assistant *should* do. The reason not to build for it today
is that the directive pointing attention there is now gone, so the observed
behaviour may simply stop. Watch for a recurrence first. If it does recur, the
cheap fix is prompt-level (Marlow's own read was that asking Mike would have
been the better instinct); a boundaries change is the expensive one and needs
the privacy question answered first, not assumed.
