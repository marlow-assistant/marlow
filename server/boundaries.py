"""Declarative security boundaries — the "OpenShell-shaped" source of truth (plan §5).

Data only. Enforcement lives in:
  - settings.json permissions.allow/deny (path-based, prefix-match — gate #2)
  - hooks/pretooluse.py (argument-shape inspection settings.json can't do — gate #4)
  - deploy/egress.nft (nftables, bound to the service's dedicated user)

Concrete paths below were verified to exist during gate #2 (see
docs/marlow-v2-gate2-results.md) rather than guessed.
"""
from __future__ import annotations

import os
import re

from server.config import settings

# --- Filesystem: the assistant's normal read/write area -------------------

WORKING_TREE = settings.working_dir

# Credential/secret paths: a "read-only" tool reading these is the
# exfiltration risk, since this agent also holds network + messaging tools.
# Wins over any allow, regardless of scope.
SECRET_PATHS: tuple[str, ...] = (
    str(settings.credentials_path),          # ~/.claude/.credentials.json (OAuth tokens)
    "~/.ssh",                                 # authorized_keys, known_hosts, private keys
    str(settings.data_dir),                   # device_tokens.json — this server's own secret store
    # APNs .p8 signing keys (docs/tasks/apns-push-delivery.md). The *directory*,
    # not just the one file MARLOW_APNS_KEY_PATH points at — Mike keeps a
    # separate .p8 per environment (sandbox + production) side by side in
    # ~/.marlow/apns/, so protecting only the configured file would leave its
    # sibling key uncovered the moment one environment is configured. Falls
    # back to the documented convention path when unset (local dev/CI).
    str(settings.apns_key_path.parent) if settings.apns_key_path else "~/.marlow/apns",
    # The tailnet TLS key (deploy/README.md §8b). The server reads it at
    # startup, outside the namespace; nothing the CLI runs needs it. Added
    # 2026-10-16 for the second instance: each instance's user holds a copy of
    # the same node key, so a turn that read it, then held another instance's
    # port across a restart, could present a valid cert for the box's name and
    # collect that instance's device tokens (docs/tasks/marlow-for-taylor.md).
    str(settings.tls_key_path.parent),
    # The admin socket's directory (config.py, `admin_socket_path`). Not a file
    # to read but a door: connecting mints pairing invites, and an invite is a
    # device token. Hidden like the token store it leads to.
    str(settings.admin_socket_path.parent),
)

# Glob suffixes checked against any path, anywhere in the tree (not just the
# working tree) — .env files show up in more than one place (gate #2 found
# both a .env in the working dir and ~/.claude/channels/discord/.env).
SECRET_GLOBS: tuple[str, ...] = (
    "**/.env",
    "**/.env.*",
)

# Files that configure the agent's OWN harness. Not secrets — the CLI must read
# every one of them — but writing any of them is a permission bypass, because
# they are all consulted BEFORE the first tool call and so before the PreToolUse
# hook exists to have an opinion.
#
# This became load-bearing on 2026-08-06 when `--strict-mcp-config` came out of
# invoke.py to restore the Gmail/Calendar connectors
# (docs/tasks/v2-dogfood-parity.md). With that flag gone the CLI honours every
# MCP config source, and an `mcpServers` entry is a `command` that runs at CLI
# STARTUP. Neither the hook nor the bwrap namespace is in that path (the network
# is deliberately not unshared), so a writable ~/.claude.json is arbitrary code
# execution outside the entire permission model.
#
# Not a new class of hazard, just the third instance: an inline `--settings`
# JSON can define hooks, which the 2026-08-03 argv spike confirmed execute
# BEFORE the CLI validates that a prompt exists.
#
# DELIBERATELY SEPARATE FROM SECRET_PATHS, and a test asserts the separation.
# That tuple now drives two consumers -- the hook's deny AND invoke.py's bwrap
# tmpfs list -- so putting these here would mount a tmpfs over the CLI's own
# config and break every turn in a way that looks like an auth bug.
AGENT_CONFIG_PATHS: tuple[str, ...] = (
    "~/.claude.json",              # user-scope MCP servers; honoured with no approval
    "~/.claude/settings.json",     # hooks, permissions -- the policy itself
    "~/.claude/settings.local.json",
    "~/.claude/plugins",           # plugin manifests are executable config
    str(settings.working_dir / ".mcp.json"),
    str(settings.working_dir / ".claude"),
    # The identity repo's own metadata, added 2026-08-08 (/security-review on
    # `feat/harness-owned-identity-git`). Same class as the entries above and the
    # fourth instance of it: `.git/config` names commands that git EXECUTES --
    # `core.fsmonitor` on `status`, `filter.<n>.clean` on `add` -- and
    # server/identity_git.py now runs both as the server process after every
    # turn, OUTSIDE the bwrap namespace that hides the secrets from the agent.
    # `--no-verify` does not cover filters or fsmonitor, and `.git/hooks` being
    # unwritable was never the whole surface.
    #
    # It was reachable with no human in the loop: `git log --output=<path>` is a
    # vetted READ that writes arbitrary bytes anywhere containment allows, and
    # `--format=%x##` synthesizes the `[`/`]`/newlines a config file needs
    # without one character outside VETTED_TOKEN_CHARS. So one auto-approved
    # command wrote .git/config and the next turn's harness commit executed it.
    # Covering the whole directory closes every write primitive into it at once,
    # rather than the one that was found.
    str(settings.working_dir / ".git"),
)

# Paths the HARNESS process legitimately touches and the agent must never
# reach. The fourth kind this file drives, after SECRET_PATHS (hard-deny +
# bwrap tmpfs), AGENT_CONFIG_PATHS (deny, deliberately NOT tmpfs'd) and the
# image-upload allow below. DENY ONLY -- there is no bwrap tmpfs for these; see
# the measurement below for why one would be redundant.
#
# Added 2026-08-30 by /security-review on `feature/schedules-and-reminders`.
# `deploy/marlow-server.service` carries `SupplementaryGroups=crontab` and
# `/var/spool/cron/crontabs` in ReadWritePaths so the schedule renderer can
# install marlow's own crontab under NoNewPrivileges. Both are properties of
# the WHOLE UNIT -- supplementary groups are process credentials, ReadWritePaths
# is a mount-namespace property -- and invoke.py spawns the CLI as an ordinary
# child of that unit, so the agent inherits the group and the writable spool.
#
# BUT MEASURED ON THE BOX 2026-08-30, and the measurement is why this stayed
# small. Under the unit's own credentials (`sudo systemd-run --uid=marlow
# --property=SupplementaryGroups=crontab`), `id -G` reports `984 997` bare and
# `984 65534` inside `bwrap --dev-bind / / --die-with-parent`. GID 997 has no
# mapping in bwrap's unprivileged user namespace, so it flattens to the
# overflow GID; the spool is 1730 root:crontab, so a CONFINED agent cannot
# traverse it, let alone append. The inheritance is real, but with bwrap on the
# kernel already holds the line.
#
# So the only live window is `bwrap_enabled` being off, and this deny is what
# covers it. A tmpfs in invoke.py was written first and then CUT (2026-08-30,
# Mike's call on complexity): with bwrap on it duplicated the user namespace,
# and with bwrap off it does not exist. It was defending only "if userns GID
# flattening ever stops holding", which is stable kernel behaviour. Do not
# re-add it without a reason that survives the probe above.
#
# Know the gap this does NOT close: the deny fires on commands that NAME a
# /var/spool/cron path, so a bare `crontab <file>` still falls through to a
# phone prompt. With bwrap on that prompt fails anyway (no group). With bwrap
# off and Mike approving it, it succeeds -- accepted, because bwrap-off is a
# dev-machine escape hatch, not the deployed box.
#
# The obvious repair -- drop the group before spawning the CLI -- does not work
# at all: setgroups(2) needs CAP_SETGID, and this server runs unprivileged as
# `marlow` under NoNewPrivileges, so the call returns EPERM.
#
# The whole /var/spool/cron directory, not just crontabs/marlow -- covering the
# parent closes every write primitive into it at once rather than the one that
# was found (the lesson recorded on `.git` above).
HARNESS_ONLY_PATHS: tuple[str, ...] = (
    "/var/spool/cron",
)

# The working-dir files that belong to the SOFTWARE, not the box
# (docs/tasks/box-upgrades.md, 2026-10-05). update.sh replaces both on every
# update, so an agent edit to either is lost at the next deploy -- and the
# CLAUDE.md one would also be the agent rewriting its own rules. Its last line
# imports PREFERENCES.md, the user's half, which stays approvable like SOUL.md.
#
# WRITE-DENY ONLY, for the structured file tools, and a fifth kind for that
# reason. Unlike AGENT_CONFIG_PATHS these are meant to be read: CLAUDE.md is
# the agent's own instructions and the app shows it, so folding them into that
# tuple would hide them from both. And unlike that tuple nothing here runs
# before the hook: these are instructions, not config the CLI executes. So the
# read/write split the AGENT_CONFIG_PATHS docstring refuses for a SHELL string
# is safe to draw by TOOL NAME here, and Bash is left to the phone prompt
# bash_policy already gives any write. That is a backstop rather than a wall,
# and enough for a file the next update overwrites anyway.
#
# Shipped job prompts joined them 2026-10-06 (box-upgrades.md "Still open" #2):
# update.sh installs the prompt of every job in its SHIPPED_JOBS list, for the
# same reason. Whether the job RUNS is the user's (its cron/schedules.json
# entry); what it does is the software's. What it needs to know about the
# user (who is close, what is live) it reads from USER.md and memory.
PRODUCT_OWNED_PATHS: tuple[str, ...] = (
    str(settings.working_dir / "CLAUDE.md"),
    str(settings.working_dir / "FIRST-RUN.md"),
    str(settings.working_dir / "cron" / "email-flash.md"),
    str(settings.working_dir / "cron" / "ideas.md"),
    # Product-owned since the harness took over scheduling it
    # (docs/tasks/default-infrastructure.md): otherwise Marlow could empty the
    # prompt of a job it can no longer delete.
    str(settings.working_dir / "cron" / "consolidate.md"),
)

# Where an uploaded image lands (docs/tasks/ios-image-input.md). An ALLOW, not
# a deny -- the third kind this file drives, after SECRET_PATHS (hard-deny)
# and AGENT_CONFIG_PATHS (write-deny). A separate tuple rather than folded
# into either: SECRET_PATHS drives invoke.py's bwrap tmpfs list, and mounting
# a tmpfs over the one place the CLI is supposed to read the image from would
# produce a "file not found" that looks identical to every other containment
# bug. server/file_policy.py is the consumer, Read only -- nothing ever writes
# here except the server process itself, outside the CLI's reach entirely.
ATTACHMENT_ROOT: tuple[str, ...] = (
    str(settings.attachments_dir),
)

# The structured tool_input keys that carry a filesystem path. ONE canonical
# tuple, because three consumers must agree about it and until 2026-08-07 there
# were three separately-maintained copies: the hook's secret-path scan, the path
# vetter's candidate list, and the pathless vetter's "you are not pathless
# anymore" guard. A key added to one and not the others is silent -- a path that
# gets secret-scanned but never containment-checked, or vice versa.
#
# Consumers are free to ADD keys (Glob's `pattern` is a path in disguise; the
# pathless guard adds `cwd`); what they may not do is keep a private copy of
# this list.
PATH_ARG_KEYS: tuple[str, ...] = ("file_path", "path", "notebook_path")

# Argument shapes that turn a "read-only" or otherwise-innocuous tool into a
# destructive/exfiltration primitive. Compiled for hooks/pretooluse.py to test
# against a Bash tool_input's command string (gate #4) — settings.json prefix
# matching can't express these.
DENY_ARG_SHAPES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bfind\b.*-(exec|execdir|delete|ok|okdir)\b"),
    re.compile(r"\brm\b\s+-[a-zA-Z]*r[a-zA-Z]*f|\brm\b\s+-[a-zA-Z]*f[a-zA-Z]*r"),  # rm -rf / -fr (any order)
    re.compile(r"\bgit\s+push\b.*(--force\b|-f\b)"),
)


# --- Bash vetting: the allowlist half (logic in server/bash_policy.py) -----
#
# Everything above is a DENYLIST -- a bounded set of things refused even when
# Mike would approve them. That shape is correct for secrets and wrong for
# everything else, and /security-review 2026-08-05 showed why: `Bash(git
# diff:*)` in settings.json read ~/.ssh via `git diff --no-index`, because a
# denylist can only refuse the verbs someone thought to name. The lists below
# invert it. They decide what the hook may clear LOCALLY, with no phone
# round-trip; anything unrecognised still goes to the phone, so this is a
# convenience boundary, not a security one. The security boundary is that no
# vetted command may name a path outside the working tree.
#
# Probed 2026-08-05 (scripts/probe-allow-vs-hook.sh runs 4-5): a hook `deny`
# binds over `permissions.allow`, so the hook genuinely gets the last word and
# this is a real enforcement point rather than a suggestion.

# `grep` was added here on 2026-08-06 and REMOVED the same day by
# /security-review. Do not add it back. The reasoning for adding it -- "it
# grants nothing the Grep tool doesn't already have vetted in file_policy.py" --
# was wrong on both halves: the Grep TOOL is ripgrep, which skips hidden files
# by default, and file_policy anchors a relative path to the tree before the
# secret check, so `Grep(path=".env")` prompts. Bash `grep` does neither.
#
# The deeper reason is a property of this list, not of grep: every verb here
# returns NAMES (ls), COUNTS (wc), or repo METADATA (git). grep would have been
# the first to return file CONTENTS, and containment cannot bound it -- `grep -r
# . .` names no target at all, so the token scan has nothing to inspect while
# the command reads every file in a tree that contains a live .env. A verb that
# prints contents needs a different rule than a verb that prints names.
#
# **That rule now exists -- CONTENT_VERBS below -- and grep still does not
# belong in THIS set.** The distinction is the whole design: a verb in
# VETTED_COMMANDS is cleared on containment alone, which is why it may only
# print names, counts or metadata. A verb in CONTENT_VERBS is cleared only when
# it NAMES targets that each resolve to an existing regular file. `grep -r . .`
# is still refused, by the second rule, for naming no target. Moving grep up
# here would restore exactly the hole /security-review found.
#
# `echo` joined 2026-09-06 and needs the invariant restated, because it returns
# neither names nor counts nor metadata: it returns ITS OWN ARGUMENTS, and so
# discloses nothing about the filesystem at all. It is here because Marlow uses
# it constantly as a separator between commands (17 occurrences in the replay
# corpus), where it was the only unvetted segment in an otherwise clean
# sequence. Containment still applies to its arguments, which costs a prompt on
# `echo /etc/passwd` -- a pointless command, and not worth an exception for.
# The invariant this set now enforces: a verb here may report NAMES, COUNTS,
# repo METADATA, or nothing at all -- never file CONTENTS.
VETTED_COMMANDS: frozenset[str] = frozenset({"git", "ls", "wc", "date", "echo"})

# --- Content verbs: cleared by NAMED TARGETS, never by containment alone ------
#
# Added 2026-09-06 after measuring, not after arguing. Marlow cannot be routed
# to the `Grep` tool: it is deferred behind `ToolSearch` while `Read` is eager,
# no flag promotes it, and across 106 `ToolSearch` calls Marlow has never once
# loaded it (docs/tasks/marlow-harness-affordances.md, CLOSED by that
# disproof). "The Grep tool plus a prompt" was the answer above until it was
# measured; it is not an available answer, so the prompt is forever.
#
# THE RULE, and every clause of it is load-bearing: a content verb is vetted
# only when it names AT LEAST ONE target, and EVERY target resolves to an
# EXISTING REGULAR FILE inside the working tree that is not a secret, agent
# config, or harness-only path.
#
#   - at least one target  -- kills `grep -r . .` and bare `cat` (which would
#                             also hang on stdin until the 3-minute Bash cap).
#   - every target         -- `cat notes.md ../../etc/passwd` is one command.
#   - existing             -- a path that does not exist yet is a path whose
#                             meaning is decided after vetting.
#   - REGULAR FILE         -- the clause containment could never express. A
#                             directory target is how a content verb reads a
#                             whole tree; `cat memory/` and `grep -rn foo <tree>`
#                             both die here. Also kills /dev/stdin and fifos.
#
# Replayed against 160 real Bash calls from Marlow's transcripts before being
# written: 40 of the 143 that prompt today would clear (28%), and 18 attack
# probes all still prompt (scripts/replay-bash-vetting.py).
CONTENT_VERBS: frozenset[str] = frozenset({"cat", "grep", "head", "tail", "nl", "sed"})

# sed joined them 2026-09-11, and ONLY in the shape `sed -n '210,280p' file`.
#
# It earned a place the same way grep did: by being what Marlow actually types.
# After the 2026-09-06 content-verb rule removed 48 of 143 prompts, `sed` was
# the single prompt left in the six attended turns that followed. Both real uses
# in the whole transcript corpus (191 Bash calls) are a bare line-range print --
# `sed -n '1,55p' memory/projects.md` -- because that is `head` with a start
# line and there is no vetted way to say it (`head -280 | tail -71` is nobody's
# reflex).
#
# **The script is the danger, not the target, which is what makes sed different
# from every other verb here.** `-i` writes the file in place, the `w` command
# writes an arbitrary path, and `e` executes a shell command -- so an allowance
# that vetted only the paths would hand over a write and an exec primitive on an
# auto-approved command. Hence a closed regex over the SCRIPT (below) rather
# than a character allowlist over it, and an option set of exactly `-n`: `-e`,
# `-f` and `-i` are absent, so `sed -i 's/a/b/' notes.md` fails on its option
# before the script is ever considered.
#
# Deliberately NOT generalised. No `$` (`1,$p`), no addresses by regex, no `d`,
# no substitution -- none of them appear in the corpus, and each is a separate
# argument about what sed can be made to do. If a real command prompts, widen
# this regex by that one shape, with the replay to show it.
#
# Note it needs no VETTED_TOKEN_CHARS exemption: `,` is already on that
# allowlist, so unlike grep's pattern (see PATTERN_EXTRA_CHARS and the command
# execution /security-review found through its first draft) this allowance adds
# no new hole for a token to slip through. The regex is strictly narrower than
# the character rule, not wider.
SED_LINE_RANGE_SCRIPT = re.compile(r"[0-9]{1,9},[0-9]{1,9}p")

# nl's `-b STYLE` (body numbering), written attached. `-ba` would otherwise be
# read as the bundle `-b -a` and fail on `-a`. Closed to the three letter styles:
# `-bpREGEX` is a fourth style and takes a regex, which is not needed and not
# argued for. 2 of 190 corpus commands are `nl -ba file` (2026-10-02).
NL_BODY_STYLE_OPT = re.compile(r"-b[atn]")

# Options accepted after a content verb, KEYED BY VERB, for the same reason the
# git options are keyed by subcommand: `-f` is `--follow` to tail and
# `--file=FILE` to grep, and conflating them is how a flat set goes wrong.
#
# NOTE WHAT IS ABSENT FROM grep: `-r` and `-R`. Recursion is the whole reason
# grep was refused in 2026-08-06, and it is excluded here by omission rather
# than by a special case -- bundles are decomposed before lookup, so `-ril`
# fails on its `-r` with no extra code. `--include`/`--exclude` are absent with
# it: they are only meaningful under recursion, and their values are globs,
# which VETTED_TOKEN_CHARS refuses anyway.
#
# ABSENT FROM tail: `-f`/`--follow`. Not a security property -- a followed file
# never returns, so it would burn the full invoke.py timeout on an
# auto-approved command nobody is watching.
VETTED_CONTENT_VERB_OPTS: dict[str, frozenset[str]] = {
    "cat": frozenset({"-n", "-b", "-s", "-E", "-T", "-A", "-v", "--number"}),
    "grep": frozenset({
        "-n", "-i", "-l", "-L", "-c", "-v", "-w", "-x", "-o", "-q", "-s", "-h",
        "-H", "-E", "-F", "-a", "-m", "-A", "-B", "-C", "-e", "-f",
        "--color", "--line-number", "--ignore-case", "--files-with-matches",
    }),
    "head": frozenset({"-n", "-c", "-q", "-v"}),
    "tail": frozenset({"-n", "-c", "-q", "-v"}),
    "nl": frozenset({"-b", "-n", "-w", "-s"}),
    # Exactly one option. See SED_LINE_RANGE_SCRIPT for why this set is closed
    # rather than merely curated -- `-e`, `-f` and `-i` are each a write or exec
    # primitive, and omission is what refuses them.
    "sed": frozenset({"-n"}),
}

# Content-verb options that consume the NEXT token as a value, KEYED BY VERB
# and tagged with what that value IS. Both halves matter and the first draft of
# this got both wrong:
#
#   - keyed by verb, because `-n` is a COUNT to head/tail and the boolean
#     `--line-number` to grep. A flat set consumed grep's pattern as `-n`'s
#     value and made `grep -n pattern file.md` -- the single most common command
#     in the corpus -- score as naming no target.
#   - tagged, because a count must be SKIPPED and a path must be CHECKED.
#     `grep -f patterns.txt data.md` reads two files; skipping the first would
#     be a hole, and treating `-A 5`'s `5` as a path would be a spurious prompt.
#
# "pattern" is a regex, never a path -- but note it is skipped rather than
# vetted, so a `-e` value is not containment-checked. That is correct (it never
# reaches the filesystem) and it is the one place here where the parse has to be
# right rather than merely conservative.
# The ONLY characters a grep PATTERN may carry beyond VETTED_TOKEN_CHARS.
#
# Deliberately two characters and not "the allowlist does not apply here". The
# first version of the exemption was written the wide way and /security-review
# 2026-09-06 (self-review, before merge) found arbitrary command execution
# through it in one step:
#
#     grep -A \'1;id \' f.md g.md
#
# `\'` is a LITERAL QUOTE to the shell, so everything between the two of them is
# unquoted and the `;` is a command separator -- the shell runs `grep -A '1` and
# then `id`. bash_policy's own quote scanner does not implement backslash
# escaping, so it reads the same `\'` as opening a quoted region, sees no
# operator, and hands the whole thing over as one segment. The character
# allowlist had always been what caught that class; exempting a token from it
# removed the backstop. With the exemption narrowed to `\` and `|`, the `'` and
# `;` in that token fail again and it prompts.
#
# See _decompose, which additionally refuses backslash-before-quote outright.
# Two independent fixes for one hole, deliberately: this module's history is
# three /security-review passes each finding a bypass of the pass before it.
#
# `#` joined 2026-10-02 (`grep -n "^#\|^##" notes.md` -- heading searches). The
# shell gives it meaning only at the START of an unquoted word, where it opens a
# comment to end of line, and newlines are already refused, so the comment can
# only reach the end of the command. A comment makes the shell run LESS than
# this module vetted -- the args after it, and any `;`-sequenced command after
# them, are dropped -- and grep left without a pattern prints usage and exits 2
# (observed 2026-10-02); left with a pattern but no files it reads stdin, which
# invoke.py pins to /dev/null. Never more, which is the direction that matters. Non-ASCII letters joined with it, but outside this set: see
# bash_policy._pattern_stray_characters.
PATTERN_EXTRA_CHARS: frozenset[str] = frozenset("\\|#")

CONTENT_VERB_VALUE_OPTS: dict[str, dict[str, str]] = {
    "cat": {},
    "grep": {
        "-m": "count", "-A": "count", "-B": "count", "-C": "count",
        "-e": "pattern", "-f": "path",
    },
    "head": {"-n": "count", "-c": "count"},
    "tail": {"-n": "count", "-c": "count"},
    "nl": {"-b": "count", "-n": "count", "-w": "count", "-s": "count"},
    # sed's only vetted option is the boolean `-n`; nothing consumes a value.
    "sed": {},
}

# --- Shape allowances: the structures Marlow writes that mean nothing ---------
#
# Measured separately (scripts/replay-bash-vetting.py) and the numbers are the
# argument for shipping these WITH the content-verb rule rather than as the
# cheap interim they look like. Alone: stderr suppression 2.1%, sequencing 0%,
# pipe sinks 1.4%. Together with the content-verb rule: 28%. They are
# superadditive because Marlow writes `cat notes.md 2>/dev/null | head -20`,
# which needs three allowances before it clears and today fails all three.
#
# A sink is a verb that TRUNCATES a stream and opens nothing. It is vetted only
# when it names no path at all, so `| tee out.md` and `| wc -l secrets` are not
# sinks. This is why the set overlaps CONTENT_VERBS without contradicting it:
# `head -20 file.md` is a content verb reading a named file; `| head -20` is a
# sink reading a pipe.
PIPE_SINK_COMMANDS: frozenset[str] = frozenset(
    {"head", "tail", "wc", "sort", "uniq", "cut", "nl", "tr", "column", "rev",
     "grep", "cat"}
)

# `grep` and `cat` joined the sinks 2026-09-11, and they are the reason
# SINK_BARE_ARGUMENTS exists.
#
# A sink is "reads stdin, opens nothing". `cat` with no operand is exactly that
# -- `| cat` is how a pager-less pipeline is spelled and it names no path at
# all. `grep` is too, but it was excluded by an ACCIDENT rather than a decision:
# the sink rule refuses any bare argument, and grep's bare argument is its
# PATTERN, not a file. That is the same misread the content-verb rule already
# had to correct in _content_verb_targets (where it once scored
# `grep -n "Cabot Trail" travel.md` as naming a file called `"Cabot`).
#
# So the budget below is one bare token for grep and zero for everything else.
# The second bare token IS a file -- `| grep pat secrets.txt` reads something
# the containment rule never saw -- so the budget is what keeps this from
# becoming a weaker second copy of that rule, exactly as "no bare arguments"
# did before it.
#
# NOTE WHAT IS ABSENT FROM grep's sink options, and why each omission is
# load-bearing rather than tidy:
#   - `-r`/`-R`. A grep with no file operand and `-r` does not read stdin at
#     all; it recurses the CURRENT DIRECTORY. That is the whole reason grep was
#     refused outright in 2026-08-06, and in sink position there is no target
#     list for containment to check, so omission is the only thing stopping it.
#   - `-f`. Reads its patterns FROM A FILE: `| grep -f /etc/passwd` is a read of
#     an arbitrary path wearing an option.
#   - `-e`. Supplies the pattern by option, which would make the one bare token
#     a FILE instead. Excluding it keeps "the bare token is the pattern" true by
#     construction rather than by parsing.
# grep has no output option, so unlike `sort -o` there is no write primitive to
# exclude here.
SINK_BARE_ARGUMENTS: dict[str, int] = {"grep": 1}

# Sink options that CONSUME the next token as a value -- the sink analogue of
# CONTENT_VERB_VALUE_OPTS, and it exists because omitting it was a critical hole
# (/security-review 2026-09-11, adversarial sub-agent, found by differential
# execution against real bash rather than by reading).
#
# _strip_pipe_sinks folds a following all-digit token into the preceding option
# (`head -n 20` -> `-20`). That fold used to apply to ANY dash token, which was
# inert only because no sink could carry a bare argument. The moment grep got a
# budget of one, it became an arbitrary out-of-tree file read:
#
#     cat notes.md | grep -v 999999 /etc/passwd
#
# `-v` is BOOLEAN, but the fold ate `999999` as its value, so the budget was
# never spent on the pattern -- and `/etc/passwd` spent it instead, passing the
# character allowlist cleanly. Real grep parses the same argv the other way:
# boolean `-v`, pattern `999999`, FILE `/etc/passwd`. `-v` inverts, so a digit
# pattern matching nothing dumps the entire file. `.env`, `.claude/settings.json`
# and `.git/config` all reached ALLOW this way -- the sink path never calls
# reaches_protected_path, so this re-opened the 2026-08-06 `wc -l .env` finding
# through a pipe.
#
# So: fold ONLY for an option that genuinely takes a value. A boolean can no
# longer absorb the token that should have spent the budget.
SINK_VALUE_OPTS: dict[str, frozenset[str]] = {
    "head": frozenset({"-n", "-c"}),
    "tail": frozenset({"-n", "-c"}),
    "sort": frozenset({"-k"}),
    "cut": frozenset({"-d", "-f", "-c", "-b"}),
    "nl": frozenset({"-b", "-n", "-w", "-s"}),
    "grep": frozenset({"-m", "-A", "-B", "-C"}),
    # `cat`, `wc`, `uniq`, `tr`, `column`, `rev` take no option values at all.
}

# Options a sink may carry, KEYED BY SINK. Required for the same reason
# VETTED_GIT_SUBCOMMAND_OPTS is: a read verb is also a write primitive.
# `cat f.md | sort -o/tmp/pwned` writes an arbitrary file, and the first draft
# of the sink rule cleared it by accepting any token beginning with `-` as an
# option (/security-review 2026-09-06, self-review before merge). That is the
# same finding as `git log --output=` in 2026-08-28, one verb later.
#
# So: no output options anywhere in this table -- `sort -o`, `sort --output`,
# `tee`'s entire purpose -- and a sink is still required to name no bare
# argument, which is what stops `sort -o /tmp/pwned` in its separate-token form.
# Options are decomposed by _short_option_forms before lookup, so an attached
# value that is not a plain count (`-o/tmp/pwned`) fails to resolve to a known
# flag and is refused rather than guessed at.
VETTED_SINK_OPTS: dict[str, frozenset[str]] = {
    "head": frozenset({"-n", "-c", "-q", "-v"}),
    "tail": frozenset({"-n", "-c", "-q", "-v"}),
    "wc": frozenset({"-l", "-w", "-c", "-m", "-L"}),
    "sort": frozenset({"-n", "-r", "-u", "-f", "-b", "-h", "-V", "-k", "-s"}),
    "uniq": frozenset({"-c", "-d", "-u", "-i"}),
    "cut": frozenset({"-d", "-f", "-c", "-b", "-s"}),
    "nl": frozenset({"-b", "-n", "-w", "-s"}),
    "tr": frozenset({"-d", "-s", "-c"}),
    # See SINK_BARE_ARGUMENTS for why `-r`, `-R`, `-e` and `-f` are absent.
    "grep": frozenset({
        "-n", "-i", "-v", "-w", "-x", "-o", "-q", "-s", "-h", "-H", "-E", "-F",
        "-a", "-m", "-A", "-B", "-C", "-c", "-l", "-L",
        "--color", "--line-number", "--ignore-case",
    }),
    "cat": frozenset({"-n", "-b", "-s", "-E", "-T", "-A", "-v", "--number"}),
    "column": frozenset({"-t", "-s"}),
    "rev": frozenset(),
}

# `git` is here because Marlow reviews the history of its own memory files, and
# Mike would thumb "allow" on nearly every one -- a prompt that is always
# approved teaches him to stop reading it. Subcommands that write to the object
# store or the network (commit, push, fetch, checkout, reset) are deliberately
# absent: those are worth a prompt.
# `tag` was here until 2026-08-28 and should never have been: `git tag <name>`
# WRITES a ref, and needs no options at all to do it, so the option allowlist
# below cannot reach it (/code-review medium). Removing it has a consequence
# worth stating, because this set is inverted by
# `bash_policy.command_is_git_write`: `git tag` in any form is now a HARD DENY,
# not a prompt. That is the right direction for a write verb, and the loss --
# `git tag --list` on a memory repo with no tags -- is nothing. Mike can run it
# over SSH.
VETTED_GIT_SUBCOMMANDS: frozenset[str] = frozenset({
    "status", "log", "diff", "show", "branch", "blame", "shortlog",
    "rev-parse", "describe",
})

# Options accepted AFTER a vetted subcommand, KEYED BY SUBCOMMAND. Added
# 2026-08-28 (docs/tasks/vetted-git-write-primitive.md), because the vetted READ
# verbs are also write primitives: `git log|show|diff --output=<file>` writes a
# file and `--format=%x..` supplies the bytes. Containment was the only test
# applied, so a path inside the working tree cleared with no prompt -- a write
# performed by a verb vetted for reading, skipping the `memory/`-only rule
# file_policy enforces on Edit/Write.
#
# NOT a denylist of `--output`. git spells it more than one way (`-o`,
# format-patch's `--output-directory`), and this module's history is two
# denylists that each omitted a case: the verb denylist that let `git diff
# --no-index` read ~/.ssh, and the metacharacter denylist VETTED_TOKEN_CHARS
# replaced. Enumerating what is safe is the shape that has held.
#
# **PER-SUBCOMMAND, and that is the load-bearing part.** The first version was
# one flat set, and `/code-review medium` found three holes in it that were all
# the same mistake: a short option means different things to different
# subcommands, so an entry chosen while thinking about one silently applied to
# all ten.
#
#   * `-u` was added for `git status --untracked-files`; on `git branch` it is
#     `--set-upstream-to`, which WRITES `.git/config` -- the file
#     AGENT_CONFIG_PATHS exists to keep unwritable, and no argument check can
#     see it because the path never appears in argv.
#   * `-M` was added for `git diff --find-renames`; on `git branch` it is
#     `--move --force`, an unprompted ref rename.
#   * `-a` was added for `git branch --all`; on `git tag` it is `--annotate`.
#
# The tell was that `--set-upstream-to=origin/main` prompted correctly while its
# own shorthand `-u` did not. Keying by subcommand makes that asymmetry
# impossible to reintroduce: an option is only ever read in the one context it
# was reasoned about.
#
# WIDEN FROM REAL DENIALS IN THE LOG, not from imagination. A missing entry is a
# prompt, which is visible and cheap; a guessed-in entry is unreviewed surface,
# and the three above are what that costs.

# Safe in every context, so exempt from the per-subcommand reasoning above.
_GIT_OPTS_EVERYWHERE: frozenset[str] = frozenset({"--no-color"})

# Shared by the three diff-shaped subcommands (log, diff, show). Kept as one
# name so the three cannot drift apart, NOT as a licence to widen all three at
# once -- anything added here must be safe on all of them.
_GIT_OPTS_DIFF_FORMATTING: frozenset[str] = frozenset({
    "--stat", "--shortstat", "--numstat", "--name-only", "--name-status",
    "--summary", "--patch", "-p", "--no-patch", "-s", "--unified", "-U",
    "--word-diff", "--find-renames", "-M", "--relative",
})

# Commit-formatting options, shared by log and show.
_GIT_OPTS_COMMIT_FORMATTING: frozenset[str] = frozenset({
    "--oneline", "--format", "--pretty", "--abbrev-commit", "--date", "--graph",
    "--decorate",
})

VETTED_GIT_SUBCOMMAND_OPTS: dict[str, frozenset[str]] = {
    "log": _GIT_OPTS_EVERYWHERE | _GIT_OPTS_DIFF_FORMATTING | _GIT_OPTS_COMMIT_FORMATTING | frozenset({
        "-n", "--max-count", "--skip", "--since", "--after", "--until",
        "--before", "--author", "--committer", "--grep", "--reverse",
        "--follow", "--all", "--merges", "--no-merges", "--first-parent", "-L",
    }),
    # NOT `--no-index` (/security-review 2026-08-05's escape to ~/.ssh), and NOT
    # `--ext-diff`/`--textconv`, which run diff drivers named in .git/config.
    "diff": _GIT_OPTS_EVERYWHERE | _GIT_OPTS_DIFF_FORMATTING | frozenset({
        "--cached", "--staged",
    }),
    "show": _GIT_OPTS_EVERYWHERE | _GIT_OPTS_DIFF_FORMATTING | _GIT_OPTS_COMMIT_FORMATTING,
    "status": _GIT_OPTS_EVERYWHERE | frozenset({
        "--short", "-s", "--branch", "-b", "--porcelain", "--untracked-files",
        "-u", "--long",
    }),
    # Read-only inspection only. `-u`/`--set-upstream-to`, `-m`/`-M` (rename),
    # `-d`/`-D` (delete), `--force` and `--edit-description` are all writes and
    # are deliberately absent.
    "branch": _GIT_OPTS_EVERYWHERE | frozenset({
        "--list", "-l", "--all", "-a", "--remotes", "-r", "--verbose", "-v",
        "--contains", "--merged", "--no-merged", "--sort", "--format",
    }),
    "blame": _GIT_OPTS_EVERYWHERE | frozenset({
        "-L", "--line-porcelain", "-w", "--show-name", "-e", "--date",
    }),
    "shortlog": _GIT_OPTS_EVERYWHERE | frozenset({
        "--summary", "-s", "--numbered", "-n", "--email", "-e", "--format",
        "--since", "--until", "--author",
    }),
    "rev-parse": _GIT_OPTS_EVERYWHERE | frozenset({
        "--short", "--abbrev-ref", "--verify", "--show-toplevel",
        "--is-inside-work-tree",
    }),
    "describe": _GIT_OPTS_EVERYWHERE | frozenset({
        "--tags", "--always", "--dirty", "--long", "--contains",
    }),
}

# Options accepted BEFORE the subcommand. `git -c core.pager=<cmd>` and
# `--exec-path` are arbitrary code execution, so this slot gets an explicit
# allowlist rather than the general path vetting.
VETTED_GIT_GLOBAL_OPTS: frozenset[str] = frozenset({"--no-pager", "--paginate"})

# Characters a token may contain and still be vetted. An ALLOWLIST, because the
# denylist this replaces ("$`><|;&\n()") omitted brace expansion, and
# `wc -l {,}/home/marlow/.ssh/id_ed25519` expands to an absolute path while the
# literal token looks like a contained relative one -- clearing both the secret
# scan and the containment check, on the one path that skips human review
# (/security-review 2026-08-05, second pass). Enumerating metacharacters is the
# same losing shape that let `git diff` through the verb denylist a commit
# earlier; this enumerates what is safe instead.
#
# Failing this does NOT deny -- vetting is abandoned and the call goes to the
# phone, so `cat foo | wc -l` stays approvable. Denying would block Mike too.
#
# Included deliberately: `^` (`HEAD^`), `%` and `+` (`--format=%h`, `date +%Y`),
# `:` (`HEAD:path`), `~` (expanded and containment-checked, so `~/.ssh` prompts).
#
# Glob characters `* ? [ ]` are NOT here. An earlier version admitted `*` and
# `?` on the reasoning that pathname expansion can only produce paths, so
# containment would still cover them. The first half is true and the second is
# not: containment resolves the *literal token*, and when the token is a glob
# the paths the shell actually opens are never resolved at all. `wc -l *`
# resolves `<tree>/*` -- a non-existent path inside the tree -- and is cleared,
# after which the shell expands it to include any symlink pointing out
# (/security-review 2026-08-05, third pass). The realpath half of containment
# only ever protected literal paths. Globbing costs one prompt, and the vetted
# set barely needs it: Glob and Grep are separate path-scoped tools.
VETTED_TOKEN_CHARS: frozenset[str] = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    " -_./=:%,@+^~"
)

# Checked against the RAW command, not the tokens: shlex.split() consumes
# newlines as ordinary whitespace, so `git status\ncurl evil` tokenises to four
# individually blameless tokens while the shell runs two commands.
UNVETTABLE_RAW_CHARS: str = "\n\r"


# --- The built-in tool surface ------------------------------------------------
#
# Passed to the CLI as `--tools=` by server/invoke.py, which carries the probe
# results and the --strict-mcp-config comparison. Two things about this list are
# easy to get wrong:
#
#   - `Grep` and `Glob` are the POINT. Without --tools the CLI does not offer
#     them at all -- 54 tools, neither among them, at CLI 2.1.251 -- so Marlow
#     could not reach them however it was coached. They are also the only
#     affordance that answers "which file mentions X", which `Read` cannot
#     substitute for and which is what every unrecoverable cron denial in the
#     transcripts was asking (4 of 4).
#   - This governs BUILT-INS ONLY. The Claude.ai connectors are unaffected, so
#     Gmail and Calendar survive -- this is not --strict-mcp-config by another
#     name, and that was verified rather than assumed.
#
# What is deliberately ABSENT, beyond the harness scaffolding the CLI stops
# offering once this is set: nothing that writes outside the file tools. Adding
# a name here widens what the model may reach for BEFORE hooks/pretooluse.py
# ever sees it, so it is a decision, not a completeness fix -- the same standing
# rule as deploy/marlow-settings.json's allow list.
#
# `TodoWrite` is absent because it DOES NOT EXIST at this CLI version. It is
# still named in marlow-settings.json's allow list, where it has never matched
# anything -- the fifth instance of a dead rule in that file (2026-09-06).
VETTED_BUILTIN_TOOLS: tuple[str, ...] = (
    "Bash",
    "Edit",
    "Read",
    "Write",
    "Grep",
    "Glob",
    "ToolSearch",
    "Skill",
    "WebSearch",
    "WebFetch",
)


# --- Pathless tools: the third vetted-allow set ------------------------------

# Tools that take no path argument at all, and so match neither vetter: Bash
# hides its paths in a command string, the file tools put theirs in a structured
# key, and these have none. Before this set existed they fell straight through
# to `request_decision`, which is why `WebSearch` and `WebFetch` raised phone
# prompts on the box despite both sitting in `permissions.allow` (2026-08-06).
#
# The general rule, learned three times now -- for Bash, for the path tools, and
# here: **an `allow` entry in settings.json is inert unless the hook has an
# independent reason to agree with it.** This set is that reason for the tools
# below, and `tests/test_marlow_settings.py` keeps the two in lockstep.
#
# Pathless BY CONSTRUCTION. Containment is the whole safety story for the other
# two sets, and it cannot be told here -- there is nothing to resolve. So a tool
# only belongs here while it carries no path; if one grows a path argument it
# moves to file_policy.py, where the containment rule lives. `vet_pathless_tool`
# enforces that at runtime rather than trusting this comment: a call carrying a
# path-shaped key abandons vetting and goes to the phone.
#
# `WebFetch` is here by Mike's explicit call (2026-08-07), choosing option 2 of
# three. It is the one entry that is a security decision rather than a repair,
# and what it removes differs by context -- state both, because the attended
# half is the milder one:
#
#   * ATTENDED: it removes a phone prompt. With §9 egress still deferred and
#     unenforced, that prompt was the only control between a prompt-injected
#     page and an exfiltration GET.
#   * UNATTENDED (cron): it removes an AUTO-DENY. A cron WebFetch matched
#     `allow` before this, but the hook fires for allow-matched calls and its
#     decision binds (probe runs 4-5), so it fell through to request_decision,
#     which with no phone to answer times out and DENIES. WebFetch was
#     effectively unavailable in cron; it is now available. That is the run
#     where the exposure is real -- the morning job reads Mike's inbox, so
#     injected text and an outbound GET meet with nobody in the loop.
#
# So §9 is no longer a cheap deferral; it is the thing that closes this. The
# allow list has named `WebFetch` since it was written, but the hook never
# honoured it, so this is the first time that entry is live in either context.
# Reverting is a one-line change here plus the two settings entries -- see the
# policy doc before doing so.
VETTED_PATHLESS_TOOLS: frozenset[str] = frozenset({
    # Web. WebSearch is a query to one search backend; WebFetch is the open one.
    "WebSearch",
    "WebFetch",
    # `TodoWrite` was here until 2026-09-06 and is deliberately gone: the CLI
    # DOES NOT OFFER IT at 2.1.251. Read from the init event's own `tools`
    # array, which lists 54 tools and not that one. So the entry vetted a tool
    # that could never be called, and its twin in marlow-settings.json's allow
    # list had never matched anything either -- inert on both layers at once,
    # which is why the lockstep test between them did not notice. That is the
    # fifth dead rule found in this pair of files, and the first that no CLI
    # warning would ever have surfaced: the warnings cover path rules whose
    # tool spelling is wrong, not names for tools that do not exist.
    # test_no_allow_entry_names_a_tool_the_cli_is_not_given now covers the gap.
    # If a future CLI reintroduces it, add it back to BOTH lists and to
    # VETTED_BUILTIN_TOOLS -- three places, deliberately, because each is a
    # separate decision.
    # Loads tool schemas. An addition rather than a repair -- it was in neither
    # settings file, and the deployed CLI defers tools behind it, so without it
    # Marlow cannot reach the connectors at all (and reported Calendar
    # "disconnected" on smoke turn 2 as a result). The tools it reveals still
    # face the hook individually, so this grants discovery, not use.
    "ToolSearch",
    # The read-only halves of the Gmail/Calendar Claude.ai connectors. The
    # writing tools are absent on purpose and fall through -- to the phone when
    # attended, to an auto-deny in cron. See marlow-settings.json's
    # _connector_comment for why they are enumerated instead of server-scoped.
    #
    # `get_thread` and `get_message` are ALSO absent, and that is a second line
    # rather than the same one (Mike, 2026-08-07): reading is not one privilege.
    # `search_threads` returns the Gmail list view -- senders, subjects,
    # snippets -- and that is the whole standing grant. Full message bodies are
    # a per-occasion decision he wants to keep making, as he did on v1, where he
    # denied `get_thread`. It is also the narrower setting for a reason this
    # session sharpened: bodies are the richest prompt-injection carrier on the
    # box, and cron WebFetch is now unprompted, so the two grants compound.
    "mcp__claude_ai_Gmail__search_threads",
    "mcp__claude_ai_Gmail__list_labels",
    "mcp__claude_ai_Google_Calendar__list_calendars",
    "mcp__claude_ai_Google_Calendar__list_events",
    "mcp__claude_ai_Google_Calendar__search_events",
    "mcp__claude_ai_Google_Calendar__get_event",
    # Our own weather server (server/weather_mcp.py, docs/tasks/weather-mcp.md).
    # All three are read-only lookups, and the morning brief is a cron turn,
    # where a fall-through would auto-deny. Unlike the connectors there is no
    # writing half to leave out.
    "mcp__weather__geocode",
    "mcp__weather__forecast",
    "mcp__weather__route_forecast",
    # Our own onboarding server (server/onboarding_mcp.py,
    # docs/tasks/onboarding-v2.md). It takes no path, reaches nothing and
    # writes nothing: its result is the next step's text. Offered only on a
    # chat turn of a box still onboarding, and a phone prompt there would be
    # the new user's first, for nothing.
    "mcp__onboarding__step_done",
})


# --- Egress: explicit host allowlist ---------------------------------------

EGRESS_ALLOWLIST: tuple[str, ...] = (
    # Anthropic — API + auth
    "api.anthropic.com",
    "console.anthropic.com",
    # APNs
    "api.push.apple.com",
    "api.sandbox.push.apple.com",
    # Web Push services (docs/tasks/portable-web-client.md). Keep in step with
    # webpush.ALLOWED_PUSH_HOSTS, which bounds what a subscription may name.
    "fcm.googleapis.com",  # Chrome, Edge, Android
    "updates.push.services.mozilla.com",  # Firefox
    "web.push.apple.com",  # Safari
    # Open-Meteo, for server/weather_mcp.py (docs/tasks/weather-mcp.md). The
    # MCP server runs inside the CLI's namespace, so the CLI's rules apply.
    "api.open-meteo.com",
    "geocoding-api.open-meteo.com",
    # Its fallbacks when Open-Meteo is down: Environment Canada, MET Norway
    # (the UK), the US National Weather Service.
    "api.weather.gc.ca",
    "api.met.no",
    "api.weather.gov",
    # OSRM's public router, for route_forecast's `depart` mode
    # (docs/tasks/route-planning.md): road times and geometry, no key.
    "router.project-osrm.org",
    # Google APIs (Gmail/Calendar)
    "www.googleapis.com",
    "oauth2.googleapis.com",
    "accounts.google.com",
    # Tailscale control plane
    "controlplane.tailscale.com",
    "login.tailscale.com",
    # Package registry (server dependency installs)
    "pypi.org",
    "files.pythonhosted.org",
)


def _expand(path: str) -> str:
    from pathlib import Path

    try:
        return str(Path(path).expanduser())
    except RuntimeError:
        # `~nosuchuser/x`: Python 3.13+ raises here where older versions
        # returned the string unchanged. Compare the token literally instead of
        # letting the exception escape -- an unhandled raise kills the hook
        # process, and with it every check that would have run afterwards
        # (/security-review 2026-08-05, third pass).
        return path


def _under_any(path: str, roots: tuple[str, ...]) -> bool:
    """Does `path` reach any of `roots`?

    Both normalised AND symlink-resolved, because expanduser() alone is not a
    containment check. pathlib collapses `.` but deliberately leaves `..` in
    place (it can cross a symlink), so the string form of a protected file is
    trivially disguised: `~/.claude/../.claude.json` is `~/.claude.json` to
    every syscall and was not caught here until 2026-08-06.

    Each candidate is paired with a root computed the SAME way -- comparing a
    normpath'd candidate against a realpath'd root fails everything on macOS,
    where /home is autofs and realpath rewrites it (the lesson already recorded
    in bash_policy.inside_working_tree).

    `any`, not `all`, deliberately: inside_working_tree() asks "is this safely contained?"
    and must satisfy both resolutions, but this asks "does this reach something
    protected?", where either resolution matching is reason enough to deny.
    """
    expanded = _expand(path)
    for root in roots:
        root_expanded = _expand(root)
        for candidate, base in (
            (os.path.normpath(expanded), os.path.normpath(root_expanded)),
            (os.path.realpath(expanded), os.path.realpath(root_expanded)),
        ):
            if base == os.sep:
                continue  # a root of "/" would match every path on the box
            if candidate == base or candidate.startswith(base.rstrip(os.sep) + os.sep):
                return True
    return False


def is_secret_path(path: str) -> bool:
    from fnmatch import fnmatch

    if _under_any(path, SECRET_PATHS):
        return True
    return any(fnmatch(_expand(path), glob) for glob in SECRET_GLOBS)


def is_agent_config_path(path: str) -> bool:
    """A path whose CONTENTS steer the harness before any hook can run.

    Denied for reads as well as writes, deliberately. The write is the attack,
    but these files carry the permission policy and the device-pairing surface,
    and a read-only carve-out would need to distinguish a reading command from a
    writing one by inspecting a shell string -- which is precisely the analysis
    that failed four times in three /security-review passes and got the whole
    boundary moved into the kernel. Marlow has no legitimate need to read its
    own settings.json; if that changes, the answer is a prompt, not a carve-out.
    """
    return _under_any(path, AGENT_CONFIG_PATHS)


def is_harness_only_path(path: str) -> bool:
    """A path the harness process owns and the agent has no business reaching.

    Denied for reads as well as writes. Reading the spool is not itself the
    attack, but the read/write distinction here would have to be drawn by
    inspecting a shell string -- the analysis that failed four times across
    three /security-review passes and got the boundary moved into the kernel.
    Marlow has no legitimate need to touch the cron spool: it edits
    `cron/schedules.json` and the harness renders the rest.
    """
    return _under_any(path, HARNESS_ONLY_PATHS)


def is_product_owned_path(path: str) -> bool:
    """A working-dir file update.sh replaces. Callers deny WRITES only."""
    return _under_any(path, PRODUCT_OWNED_PATHS)


def matches_deny_arg_shape(command: str) -> bool:
    return any(pattern.search(command) for pattern in DENY_ARG_SHAPES)
