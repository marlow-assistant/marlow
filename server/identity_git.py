"""Harness-owned git for the identity working dir (docs/tasks/marlow-working-dir-git-policy.md).

`marlow-v2-START.md`'s hard constraints: *"Git in the identity dir is
harness-owned, not Marlow's discretion."* The server commits any diff in
`MARLOW_WORKING_DIR` after every turn; `bash_policy.command_is_git_write` denies
the model the write subcommands. Two reasons that beats instructing the model to
commit in its own CLAUDE.md, which is what v1 did:

  - It cannot be forgotten. A commit the model chooses to make is a commit it can
    skip, and `docs/tasks/marlow-harness-affordances.md` is a catalogue of Marlow
    routing around capabilities it already has.
  - It removes git from the model's tool surface. Every `git add` was a Bash
    string for `bash_policy` to vet, and vetting is where this repo's near-misses
    have lived.

**Commit messages are mechanical and carry no prompt text.** Deriving a message
from the turn would put user content in git history, which must never hold
it; and once the model
cannot run `git commit`, no turn can author a message anyway — including the
nightly consolidation turn. The semantic record is the *file content*, which is
committed either way.

**This module is the only place a secret can leave the working tree.** Everything
else the agent does passes `file_policy`/`bash_policy`; a server-side `git add`
passes neither, and piece 4 of the task doc pushes what lands here to GitHub.
Gate #2 found a real `.env` in the working dir holding a live token. So the
exclusion below is by construction — secret paths are never staged, rather than
staged and cleaned up — and it is in code rather than resting on the working
dir's `.gitignore`, which lives inside the tree the agent can write to and is one
approved hand-edit away from not covering the case.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import NamedTuple

from server.boundaries import is_agent_config_path, is_secret_path
from server.config import settings
from server.file_policy import WRITABLE_SUBTREE
from server.log import get_logger

logger = get_logger(__name__)

# The seed script hit this first: a fresh system user has no git config at all,
# and a bare `git commit` there aborts with "Author identity unknown". Passed per
# invocation with `-c` so this never depends on repo or global state.
_AUTHOR_NAME = os.environ.get("GIT_AUTHOR_NAME", "Marlow")
_AUTHOR_EMAIL = os.environ.get("GIT_AUTHOR_EMAIL", "marlow@localhost")

_TIMEOUT_SECONDS = 30

# `git status --ignored` reports ignored paths with this status code.
_IGNORED = "!!"

# Ignored paths outside the writable subtree are routine (build junk, the Trello
# sync's generated van-board files). Inside it they mean memory is not being
# backed up, so only those are worth a warning -- capped, because a single
# ignored directory can expand to thousands of files.
_IGNORED_WARN_LIMIT = 10


# Config that turns one of OUR OWN invocations into command execution. Passed on
# every call because `-c` beats the repo's config file (/security-review
# 2026-08-08, which found a live path to RCE as this process).
#
#   core.fsmonitor  -- `git status` runs it. The first thing this module does.
#   core.hooksPath  -- belt for the `--no-verify` on the commit, which covers
#                      commit hooks and nothing else.
#
# NOT a claim that git is now safe to run over an attacker-controlled repo. The
# primary control is that `<working_dir>/.git` is in `AGENT_CONFIG_PATHS`, so the
# agent cannot write the config in the first place; these two flags and the
# tamper check below are what stands up if that ever fails.
#
# PUBLIC because piece 4's pusher runs git in the same repo AS ROOT and needs the
# same treatment -- import these two rather than re-deriving them, or the two
# copies drift and the root one is the copy that matters. Read the note on
# `tampered_config_keys` first: push has exec vectors these flags do not cover.
GIT_HARDENING_FLAGS: tuple[str, ...] = (
    "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null",
)

# Config families that bring execution into `git add`/`status` and that a
# command-line `-c` CANNOT neutralise, because the key names are open-ended:
# `filter.<anything>.clean` runs when a `.gitattributes` assigns it (and
# `memory/.gitattributes` needs no permission prompt), and an `include` /
# `includeIf` directive merges in another file that can define one. Nothing this
# repo legitimately does sets either, so their presence is treated as tampering:
# skip the commit, loudly. Fail-closed is the right trade only here -- a missed
# backup is recoverable, executing an agent-chosen command as this process is not.
TAMPER_CONFIG_PREFIXES: tuple[str, ...] = ("filter.", "include.", "includeif.")


class GitConfigUnreadable(RuntimeError):
    """`git config` failed for a reason that is not "no matching keys".

    Raised rather than returned so a caller cannot mistake it for a clean result.
    Only exit **1** means "no matching keys"; anything else means we did not read
    the config, and reporting that as an absence of tamper keys is a fail-open on
    the one check that stops this process executing an agent-chosen command.

    Found via piece 4's since-reverted root pusher, where `git` exits **128** on a
    repo owned by another user ("detected dubious ownership"). That caller is gone,
    and this is still a real bug on the path below: a stalled git, or a `.git` this
    process cannot read, would report clean without having read a single key.
    Callers must treat it as a refusal, not an absence.
    """


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *GIT_HARDENING_FLAGS, *args],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        stdin=subprocess.DEVNULL,
    )


def tampered_config_keys(working_dir: Path) -> list[str]:
    """Local config keys that could execute a command during our own git calls.

    Reads the repo's config through git rather than parsing the file, so an
    `include` directive's contents are seen too -- the point is what git will
    honour, not what one file says.

    Public because it was shared with piece 4's pusher when that ran as root. It no
    longer does (`docs/tasks/marlow-working-dir-git-policy.md` records why), so this
    has one caller again -- `commit_after_turn`. If a *privileged* caller ever comes
    back, note that `-c` is not a general answer to repo-local config: three keys
    `push` reaches (`remote.<n>.receivepack`, `protocol.ext.allow`, `core.gitProxy`)
    win over the command line, which is what killed the root design.

    Fail-closed on anything it cannot read -- see `GitConfigUnreadable`.
    """
    listing = _git(["config", "--local", "--list", "--name-only", "-z"], working_dir)
    if listing.returncode == 1:
        # git's "no matching keys". A repo with no local config is not an error
        # worth stopping a commit over.
        return []
    if listing.returncode != 0:
        # Anything else means we did not read the config, which is not the same as
        # there being nothing in it -- see GitConfigUnreadable.
        raise GitConfigUnreadable(
            f"git config exited {listing.returncode} in {working_dir}: "
            f"{listing.stderr.strip() or '(no stderr)'}"
        )
    names = [name for name in listing.stdout.split("\0") if name]
    return sorted(
        name for name in names
        if name.lower().startswith(TAMPER_CONFIG_PREFIXES)
    )


def _parse_porcelain_z(payload: str) -> list[tuple[str, str]]:
    """(status, path) pairs from `git status --porcelain=v1 -z`.

    `-z` rather than the human format because it is the only variant that does
    not quote or escape unusual filenames -- a path is whatever sits between two
    NULs. A rename/copy entry spends an EXTRA field on its source path
    (`XY <new> NUL <old> NUL`), so the reader has to know that or it silently
    treats the old path as a status record and desynchronises for the rest of the
    list.

    The status travels with the path because `--ignored=matching` puts `!!`
    records in the same stream, and those must be reported without being staged.
    """
    fields = [field for field in payload.split("\0") if field]
    records: list[tuple[str, str]] = []
    index = 0
    while index < len(fields):
        record = fields[index]
        if len(record) < 4:
            break
        status, path = record[:2], record[3:]
        records.append((status, path))
        # Renames and copies carry their source as the next field. Both halves
        # matter: staging the new path records the content, and the old path
        # needs staging too so the deletion is part of the same commit.
        if "R" in status or "C" in status:
            index += 1
            if index < len(fields):
                records.append((status, fields[index]))
        index += 1
    return records


def _literal(path: str) -> str:
    """A pathspec that means this exact path and nothing else.

    git pathspecs are wildmatch patterns, not paths: a file literally named `*`
    (which Marlow can create -- a Write inside `memory/` is allowed with no
    prompt) makes `git add -- '*'` stage every sibling, including paths this
    module computed as excluded. That trips the index re-check below, which
    aborts the commit -- and because the filename persists, it would abort every
    subsequent turn's commit too, silently ending backups. `:(literal)` makes the
    staged set exactly the computed set.
    """
    return f":(literal){path}"


def _is_excluded(path: str, working_dir: Path) -> bool:
    """Secrets and the agent's own harness config never get committed.

    `is_secret_path` is glob-matched against a full path, so it is given an
    absolute one -- a bare relative `.env` matches nothing, which is the exact
    miss /security-review found in bash_policy on 2026-08-06.

    `AGENT_CONFIG_PATHS` covers `<working_dir>/.mcp.json` and `.claude/`. Not
    secrets, but they are the agent's harness rather than its identity, and
    tracking them would put a permission-relevant file where a future `git
    checkout` could rewrite it.
    """
    absolute = str(working_dir / path)
    return is_secret_path(absolute) or is_agent_config_path(absolute)


def _warn_about_ignored_memory(ignored: list[str]) -> None:
    """Say something when memory is being ignored rather than committed.

    Warn rather than force-add. `add -f` would drag back exactly the files the
    ignore file exists to keep out, and this module is not the place to overrule
    a deliberate ignore -- but an unattended box whose memory silently stopped
    being versioned is the failure this whole feature exists to prevent, and a
    log line is the difference between "nothing changed" and "nothing is being
    saved".
    """
    prefix = f"{WRITABLE_SUBTREE}/"
    in_memory = sorted(
        path for path in ignored
        if path == WRITABLE_SUBTREE or path.startswith(prefix)
    )
    if not in_memory:
        return
    overflow = len(in_memory) - _IGNORED_WARN_LIMIT
    logger.warning(
        "identity git: %d path(s) under %s/ are gitignored and are NOT being "
        "committed or backed up: %s%s",
        len(in_memory),
        WRITABLE_SUBTREE,
        ", ".join(in_memory[:_IGNORED_WARN_LIMIT]),
        f" (+{overflow} more)" if overflow > 0 else "",
    )


class TurnCommit(NamedTuple):
    """What a turn's commit recorded. `paths` are repo-relative and already
    exclude secret and agent-config paths -- they are exactly what was staged,
    which is what the activity log shows as "files changed"
    (docs/tasks/ux-lane.md §2a). A NamedTuple so it stays truthy wherever the
    old bare-sha return was only tested for truthiness."""

    sha: str
    paths: tuple[str, ...]


def commit_after_turn(turn_id: str, source: str | None = None) -> TurnCommit | None:
    """Commit whatever the turn changed. Returns the commit, or None for a no-op.

    Never raises. Durability must not be able to break a conversation: the turn
    has already run and its reply is already the user's, so a git failure here is
    logged and swallowed. The next turn's `add -A` picks up anything missed,
    which also covers losing a race for `.git/index.lock` against the nightly
    pusher.
    """
    working_dir = settings.working_dir
    if not (working_dir / ".git").exists():
        # No-op rather than an error: CI, local dev, and an unseeded box all have
        # a working dir that is not a repo. `seed-from-v1.sh` is what makes it one.
        return None

    try:
        tampered = tampered_config_keys(working_dir)
        if tampered:
            logger.error(
                "identity git: REFUSING to run -- %s local config key(s) can execute "
                "a command during `git add`/`status` and cannot be overridden on the "
                "command line: %s. The identity dir sets none of these legitimately; "
                "treat this as tampering, not misconfiguration.",
                len(tampered),
                ", ".join(tampered),
            )
            return None

        status = _git(
            [
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                # Ignored paths are reported, not staged. Change detection is
                # `git status`, which omits ignored files -- and `memory/` is
                # writable with no prompt, so a turn that writes
                # `memory/.gitignore` could otherwise opt its own memory out of
                # every future commit with nothing to distinguish that from a
                # turn which changed nothing. Respecting .gitignore is still
                # correct (Mike's ignores the Trello sync's generated files); it
                # is the SILENCE that was the hole.
                "--ignored=matching",
            ],
            working_dir,
        )
        if status.returncode != 0:
            logger.error("identity git: status failed: %s", status.stderr.strip())
            return None

        records = _parse_porcelain_z(status.stdout)
        changed = [path for code, path in records if code != _IGNORED]
        _warn_about_ignored_memory([path for code, path in records if code == _IGNORED])

        excluded = [path for path in changed if _is_excluded(path, working_dir)]
        to_stage = [path for path in changed if path not in excluded]

        if excluded:
            # Deliberately loud and deliberately not fatal. A `.env` in the
            # working tree is a real thing that has happened here; the right
            # outcome is that it stays untracked while everything else still
            # gets backed up, not that backups stop until someone notices.
            logger.warning(
                "identity git: %d path(s) excluded from the commit as secret or "
                "agent-config: %s",
                len(excluded),
                ", ".join(sorted(excluded)),
            )
        if not to_stage:
            return None

        add = _git(["add", "-A", "--", *(_literal(path) for path in to_stage)], working_dir)
        if add.returncode != 0:
            logger.error("identity git: add failed: %s", add.stderr.strip())
            return None

        staged = _git(["diff", "--cached", "--name-only", "-z"], working_dir)
        if staged.returncode == 0:
            leaked = [
                path
                for path in staged.stdout.split("\0")
                if path and _is_excluded(path, working_dir)
            ]
            if leaked:
                # Belt and braces: the explicit pathspec above should make this
                # unreachable. If it ever fires, the exclusion logic and git
                # disagree about what a path is, and the safe move is to commit
                # nothing rather than to commit a secret and push it tonight.
                logger.error(
                    "identity git: ABORTING commit -- excluded path(s) reached the "
                    "index: %s",
                    ", ".join(sorted(leaked)),
                )
                _git(["reset"], working_dir)
                return None

        label = f"{source or 'turn'} {turn_id}"
        message = f"turn: {label} ({len(to_stage)} file{'s' if len(to_stage) != 1 else ''})"
        commit = _git(
            [
                "-c",
                f"user.name={_AUTHOR_NAME}",
                "-c",
                f"user.email={_AUTHOR_EMAIL}",
                "commit",
                # The repo's own hooks would run as this server process, and
                # `.git/hooks` sits inside the working tree -- outside
                # WRITABLE_SUBTREE, so writing one prompts rather than being
                # denied. Not a path worth leaving open for a mechanical commit.
                "--no-verify",
                "-m",
                message,
            ],
            working_dir,
        )
        if commit.returncode != 0:
            logger.error("identity git: commit failed: %s", commit.stderr.strip())
            return None

        head = _git(["rev-parse", "--short", "HEAD"], working_dir)
        sha = head.stdout.strip() if head.returncode == 0 else "?"
        logger.info("identity git: committed %s (%d file(s))", sha, len(to_stage))
        return TurnCommit(sha, tuple(sorted(to_stage)))
    except GitConfigUnreadable as e:
        # Fail-closed, same as a positive tamper hit: we could not establish that
        # the config is safe, so nothing is staged or committed this turn.
        logger.error("identity git: REFUSING to run -- %s", e)
        return None
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.error("identity git: %s", e)
        return None
