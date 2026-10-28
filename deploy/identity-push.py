#!/usr/bin/env python3
"""Piece 4 of docs/tasks/marlow-working-dir-git-policy.md: push the identity
working dir to its private origin, on a timer, as `marlow`.

`server/identity_git.py` commits the brain after every turn, but nothing moves
those commits off the box. That was the original problem -- one copy, one machine
-- and it is the whole reason this file exists.

**Read `scripts/backup-identity-bundle.sh` as part of this design, not as an
extra.** This holds a write-capable deploy key, so the agent can destroy the
GitHub side of its own backup. The bundle pulled onto Mike's Mac is what makes
that acceptable; the two are one mechanism.

**Runs as `marlow`, deliberately, having been built as a root cron first.** Do not
re-privilege it: running git as root over a directory the agent can write makes
`.git/config` -- which names commands git executes -- a root-execution primitive,
and the allowlist that was meant to contain it lost to three keys that beat `-c`.
Piece 4 of the task doc has the mechanisms and the decision rule.

**What running as `marlow` buys is a smaller blast radius, not the absence of a
boundary.** Hostile `.git/config` no longer reaches root, but this process runs
OUTSIDE the bwrap namespace `invoke.py` builds, where a tmpfs hides every
`boundaries.SECRET_PATHS` entry from the agent -- `~/.ssh` (including this
script's own write-capable deploy key), `~/.marlow/data`, the APNs `.p8` dir. So
config-as-code here is a confinement escape: it hands the agent secrets the
kernel is otherwise keeping from it.

There is no tamper precheck because the *write primitive is denied*, not because
there is nothing to defend: `<working_dir>/.git` is in
`boundaries.AGENT_CONFIG_PATHS`. This is now the second consumer of that
unwritability (`server/identity_git.py` is the first), and it was already
defeated once -- `git log --output=` was a vetted read that wrote arbitrary bytes,
2026-08-08. **Decision rule: if a write primitive into `<working_dir>/.git` is
found again, this script needs hardening in the same change that closes it** --
a tamper precheck, or `GIT_CONFIG_GLOBAL`-free `-c` pinning of the keys piece 4
enumerates. Do not add that machinery speculatively before then.

The script is still installed **root-owned** to /opt/marlow/bin (755) and merely
*executed* by `marlow` -- deploy/README.md §6b: a crontab line pointing into
Marlow's own working tree would let the agent rewrite tomorrow's scheduled job.

Stdlib only, plus this repo's `server/` package, which is stdlib-only all the way
down. So this runs on /usr/bin/python3 with no venv. Install: README §7a.

## Failure reporting

A silent failure here means the brain quietly stops being backed up, which is the
one outcome this piece exists to prevent. So every refusal reports through
`/internal/cron/message` -- the only outbound channel reaching Mike's phone today.
Sound for *this* class because a rejected push says nothing about server health; a
dead server needs the out-of-band switch (docs/tasks/failure-alert-channel.md).

Every exit path other than success goes through `_fail`, including a timed-out
push -- an escaped exception would end the process without reporting, which is the
silence this file exists to remove. Exit codes are deliberately coarse, so the
`kind` in each report carries the detail:

  0  pushed, or already up to date
  1  operational -- push rejected, refused, timed out, or git itself failed
  2  precondition wrong -- no repo, no origin, no key
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from datetime import date
from pathlib import Path

# Same standalone-copy problem as cron/entrypoint.py: the installed file has no
# `server/` package next to it, so MARLOW_SERVER_REPO (set in the crontab) points
# at the software checkout. The fallback only serves running it straight out of
# the repo, in dev and tests.
_repo_root = os.environ.get("MARLOW_SERVER_REPO") or str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _repo_root)

KEY_PATH = Path(
    os.environ.get("MARLOW_IDENTITY_PUSH_KEY") or Path.home() / ".ssh" / "identity-push"
)
BRANCH = os.environ.get("MARLOW_IDENTITY_PUSH_BRANCH", "main")
REMOTE = os.environ.get("MARLOW_IDENTITY_PUSH_REMOTE", "origin")
_TIMEOUT_SECONDS = 120

JOB_NAME = "identity-push-failure"

# A push the remote *decided* to decline, as opposed to one git refused locally on
# a ref comparison (see the divergence markers in main). Two families, because they
# arrive from different places:
#
#   - `remote rejected` / `remote: error` -- the git receive side declined, e.g. a
#     branch protection rule.
#   - `read-only access` / `denied to` -- GitHub's SSH entry point declined before
#     the git protocol even started, which is what a deploy key added WITHOUT
#     "Allow write access" produces. No `remote:` prefix on those, so matching only
#     the family above would drop the single most likely first-run failure into the
#     generic "push failed" bucket, losing the one hint that resolves it.
_REFUSED_MARKERS: tuple[str, ...] = (
    "remote rejected",
    "remote: error",
    "read-only access",
    "denied to",
)


def _report_failure(kind: str, summary: str, detail: str) -> None:
    """Turn a refusal into a turn, so it reaches a human.

    Best-effort by construction, and never raises -- a broken reporter must not mask
    the failure it is reporting.

    `kind` is in the dedup key, not just the date: queue.py's client_msg_id index
    returns the first turn_id and DISCARDS the second enqueue, so a date-only key
    would let a routine morning rejection swallow an afternoon failure of a
    different kind. Same-kind repeats still collapse to one a day. Per server
    lifetime, since that index is in memory -- erring toward telling Mike twice.
    """
    from server.config import settings  # noqa: PLC0415 -- after sys.path setup
    from server.internal_client import InternalAPIError, post_json  # noqa: PLC0415

    text = (
        "Automated alert from the identity-push cron (not a message from Mike). "
        f"The hourly backup of your working directory to its private git remote failed: {summary}\n\n"
        f"Details:\n{detail}\n\n"
        "Tell Mike in one or two lines, plainly, that memory backups have stopped "
        "and what the reason above says. Do not try to reconcile git history "
        "yourself -- a stopped backup is a thing for him to look at, and rewriting "
        "history to clear it is the one action that could lose the thing being "
        "backed up."
    )
    body = {
        "job": JOB_NAME,
        "text": text,
        "client_msg_id": f"{JOB_NAME}-{kind}-{date.today().isoformat()}",
    }
    sock = settings.internal_socket_path
    try:
        post_json(sock, "/internal/cron/message", body, timeout=30)
    except InternalAPIError as e:
        print(f"identity-push: could not report failure to {sock}: {e}", file=sys.stderr)
    except ValueError:
        pass  # delivered; the reply body isn't needed


def _fail(code: int, kind: str, summary: str, detail: str) -> int:
    print(f"identity-push: {summary}\n{detail}", file=sys.stderr)
    _report_failure(kind, summary, detail)
    return code


def _run_git(args: list[str], working_dir: Path) -> subprocess.CompletedProcess[str]:
    """git, raising OSError-family errors for the caller to report.

    `subprocess.run(timeout=...)` raises rather than returning non-zero, and an
    escaped TimeoutExpired would exit this process without ever calling `_fail` --
    a hung push reporting nothing at all, which is the exact failure this script
    exists to make loud. Callers wrap it.
    """
    return subprocess.run(
        ["git", "-C", str(working_dir), *args],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        stdin=subprocess.DEVNULL,
    )


def _ssh_command() -> str:
    """`core.sshCommand` pinning the push to the identity deploy key.

    Correctness, not a boundary -- this runs as the key's owner, so the point is
    using the *right* key. `IdentitiesOnly` so marlow's default key is not offered
    instead (GitHub matches it to a different repo and declines); `BatchMode` so a
    passphrase prompt fails fast instead of hanging a cron job; `accept-new` to pin
    github.com on first connect. shlex.quote: git hands this to a shell.
    """
    return (
        f"ssh -i {shlex.quote(str(KEY_PATH))} "
        "-o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
    )


def main() -> int:
    from server.config import settings  # noqa: PLC0415 -- after sys.path setup

    working_dir = settings.working_dir

    # A missing repo is a precondition failure, NOT a quiet no-op: treating it as
    # success would mean backing up nothing, forever, with no signal.
    if not (working_dir / ".git").is_dir():
        return _fail(
            2,
            "no-repo",
            f"no git repo at {working_dir}",
            "Check MARLOW_WORKING_DIR in the crontab against the deployed "
            "working directory.",
        )

    if not KEY_PATH.is_file():
        return _fail(
            2, "no-key", f"no deploy key at {KEY_PATH}", "See deploy/README.md 'Identity push'."
        )

    try:
        remotes = _run_git(["remote"], working_dir)
    except (subprocess.TimeoutExpired, OSError) as e:
        return _fail(1, "git-error", "could not list remotes", str(e))

    # Checked, because an empty stdout from a FAILED git reads identically to "no
    # remotes" -- and telling Mike to add an origin that already exists sends him
    # somewhere useless.
    if remotes.returncode != 0:
        return _fail(
            1,
            "git-error",
            f"`git remote` failed in {working_dir}",
            remotes.stderr.strip() or f"git exited {remotes.returncode} with no output",
        )
    if REMOTE not in remotes.stdout.split():
        return _fail(
            2,
            "no-remote",
            f"no {REMOTE!r} remote in {working_dir}",
            "Add one with `git remote add origin git@github.com:...` (SSH, not "
            "HTTPS -- a deploy key is an SSH credential).",
        )

    try:
        push = _run_git(
            [
                "-c", f"core.sshCommand={_ssh_command()}",
                "push",
                # A half-written pre-push hook in the working tree must not be able
                # to stop the backups. Availability, not a boundary -- the hook and
                # this process run as the same user either way.
                "--no-verify",
                REMOTE, f"HEAD:refs/heads/{BRANCH}",
            ],
            working_dir,
        )
    except subprocess.TimeoutExpired:
        # BatchMode stops ssh prompting, but not a stalled connection. Reported
        # rather than raised: an escaped exception here exits without ever calling
        # _fail, which is a backup that stopped with nothing said about it.
        return _fail(
            1,
            "timeout",
            f"push timed out after {_TIMEOUT_SECONDS}s",
            f"git push to {REMOTE} did not finish. The remote may be unreachable "
            "from the box. Backups stay stopped until a run succeeds.",
        )
    except OSError as e:
        return _fail(1, "git-error", "could not run git push", str(e))

    output = f"{push.stdout}\n{push.stderr}".strip()

    if push.returncode == 0:
        # git says "Everything up-to-date" on stderr; both cases are success and
        # neither is worth waking anyone over.
        print(f"identity-push: ok -- {output.splitlines()[-1] if output else 'pushed'}")
        return 0

    lowered = output.lower()

    # Divergence: git compared refs and refused. Only the box pushes, so the sole
    # way to get here is an edit made somewhere else -- and the fix is a human
    # merge decision, not a retry.
    #
    # Matched on the specific markers, NOT on bare "rejected": git also prints
    # `! [remote rejected]` when the SERVER refuses a push it would otherwise
    # accept, which is a different problem with a different fix. Claiming
    # divergence there would send Mike hunting for commits that do not exist.
    if "non-fast-forward" in lowered or "fetch first" in lowered or "! [rejected]" in lowered:
        return _fail(
            1,
            "diverged",
            f"push REJECTED -- {REMOTE}/{BRANCH} has commits the box does not",
            "Treat the identity remote as write-only-from-the-box. Something (most "
            "likely a GitHub web edit) diverged it, and backups stay stopped until "
            f"it is reconciled by hand.\n\n{output}",
        )

    if any(marker in lowered for marker in _REFUSED_MARKERS):
        return _fail(
            1,
            "remote-refused",
            f"{REMOTE} REFUSED the push -- most likely the deploy key lacks write access",
            "Not a divergence: the remote reached a decision and declined. A deploy "
            "key is read-only unless 'Allow write access' was ticked when it was "
            "added, and that is the common first-run failure. Check the key's write "
            "permission on the identity repo before looking at anything else; a "
            f"branch protection rule is the other possibility.\n\n{output}",
        )

    return _fail(
        1, "push-failed", "push failed", output or f"git exited {push.returncode} with no output"
    )


if __name__ == "__main__":
    raise SystemExit(main())
