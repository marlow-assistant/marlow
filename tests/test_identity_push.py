"""deploy/identity-push.py — piece 4, the identity pusher.

One property is load-bearing, and it is asserted against a real bare remote rather
than mocks: it never reports success for a push that did not happen. Everything
this script does beyond `git push` exists to make a failed backup loud.

Notably absent, and not by oversight: no tamper-precheck test and no
`safe.directory` test. Not because hostile git config is harmless here — the
pusher runs outside `invoke.py`'s bwrap namespace, so it *can* reach the secrets
the agent cannot, this script's own deploy key among them — but because the write
primitive into `<working_dir>/.git` is denied by `boundaries.AGENT_CONFIG_PATHS`.
There is nothing for a precheck to catch that is not already unreachable, and the
version that defended it in depth (as root) cost more than it bought; see
docs/tasks/marlow-working-dir-git-policy.md. `identity_git` keeps its own
fail-closed tests for the commit path.

`identity-push.py` is hyphenated (it is an installed script, not an importable
module), so it is loaded by path here.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import safe_git_init

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git required")

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "deploy" / "identity-push.py"


@pytest.fixture
def pusher():
    spec = importlib.util.spec_from_file_location("identity_push", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# Identity passed per-command with `-c` rather than written via `git config`:
# a config write persists into whatever repo it lands in (that is how
# user.name=T reached the real .git/config), while -c cannot outlive the
# process. Fixtures should leave no trace on disk beyond their tmp dir.
_ID = ("-c", "user.name=test", "-c", "user.email=test@example.com")


def _git(repo, *args) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return out.stdout


@pytest.fixture
def box(tmp_path, pusher, monkeypatch):
    """A working dir with a real bare `origin`, wired in as settings.working_dir.

    A local bare repo means the push path is exercised for real — including the
    non-fast-forward rejection, which is the case with a human-only fix and so the
    one most worth testing rather than asserting about.
    """
    import dataclasses

    from server import config, identity_git

    origin = tmp_path / "origin.git"
    safe_git_init(origin, "--bare", "-b", "main")

    working_dir = tmp_path / "marlow-identity"
    safe_git_init(working_dir, "-b", "main")
    (working_dir / "memory").mkdir(parents=True)
    _git(working_dir, "remote", "add", "origin", str(origin))
    (working_dir / "memory" / "fact.md").write_text("first\n")
    _git(working_dir, "add", "-A")
    _git(working_dir, *_ID, "commit", "-q", "-m", "seed")

    patched = dataclasses.replace(config.settings, working_dir=working_dir)
    monkeypatch.setattr(config, "settings", patched)
    monkeypatch.setattr(identity_git, "settings", patched)

    # A local path remote needs no ssh or key; point the key at something real so
    # the precondition check passes and the ssh flags stay unexercised.
    key = tmp_path / "identity-push"
    key.write_text("not-a-real-key\n")
    monkeypatch.setattr(pusher, "KEY_PATH", key)

    reports: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        pusher, "_report_failure", lambda k, s, d: reports.append((k, s, d))
    )
    return working_dir, origin, reports


# --- the happy path ----------------------------------------------------------


def test_a_commit_reaches_the_remote(box, pusher):
    working_dir, origin, reports = box
    assert pusher.main() == 0
    assert reports == []
    assert _git(origin, "rev-parse", "main").strip() == _git(
        working_dir, "rev-parse", "HEAD"
    ).strip()


def test_nothing_to_push_is_success_not_an_alert(box, pusher):
    """An hourly cron is up to date almost every time it runs. That must be quiet."""
    assert pusher.main() == 0
    assert pusher.main() == 0
    assert box[2] == []


# --- the refusals ------------------------------------------------------------


def test_a_divergent_remote_is_reported_not_swallowed(box, pusher):
    """Only the box pushes, so divergence means someone edited the remote directly.
    A cron failing non-fast-forward in silence would stop backing up the brain with
    nobody noticing."""
    working_dir, origin, reports = box
    assert pusher.main() == 0

    # Someone commits on the remote (a GitHub web edit, in production).
    other = working_dir.parent / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    (other / "memory" / "fact.md").write_text("edited elsewhere\n")
    _git(other, "-c", "user.name=web", "-c", "user.email=web@example.com",
     "commit", "-qam", "web edit")
    _git(other, "push", "-q", "origin", "main")

    (working_dir / "memory" / "fact.md").write_text("local\n")
    _git(working_dir, *_ID, "commit", "-qam", "local change")

    assert pusher.main() == 1
    assert len(reports) == 1
    assert reports[0][0] == "diverged"
    assert "REJECTED" in reports[0][1]


def test_a_working_dir_that_is_not_a_repo_is_a_loud_failure(box, pusher, monkeypatch, tmp_path):
    """"Nothing to push" is the symptom of a misaimed MARLOW_WORKING_DIR, and
    silence there would back up nothing, forever."""
    import dataclasses

    from server import config

    empty = tmp_path / "elsewhere" / "marlow-identity"
    empty.mkdir(parents=True)
    monkeypatch.setattr(
        config, "settings", dataclasses.replace(config.settings, working_dir=empty)
    )

    assert pusher.main() == 2
    assert "no git repo" in box[2][0][1]


def test_a_missing_deploy_key_is_a_loud_failure(box, pusher, monkeypatch, tmp_path):
    monkeypatch.setattr(pusher, "KEY_PATH", tmp_path / "absent")
    assert pusher.main() == 2
    assert "deploy key" in box[2][0][1]


def test_a_missing_origin_is_a_loud_failure(box, pusher):
    working_dir, _origin, reports = box
    _git(working_dir, "remote", "remove", "origin")
    assert pusher.main() == 2
    assert reports[0][0] == "no-remote"


# --- failures that must not go unreported or be misnamed ---------------------


def test_a_hung_push_is_reported_not_raised(box, pusher, monkeypatch):
    """The worst failure this script could have: an escaped TimeoutExpired exits
    without ever calling _fail, so a stalled push stops the backups and says
    nothing. BatchMode stops ssh prompting, not a stalled connection."""
    real = pusher._run_git

    def hang(args, working_dir):
        if "push" in args:
            raise subprocess.TimeoutExpired(args, pusher._TIMEOUT_SECONDS)
        return real(args, working_dir)

    monkeypatch.setattr(pusher, "_run_git", hang)

    assert pusher.main() == 1
    assert box[2][0][0] == "timeout"
    assert "timed out" in box[2][0][1]


def test_the_dedup_key_includes_the_failure_kind(pusher, internal_stub):
    """A second failure of a different kind must not be swallowed by the first.
    server/queue.py's client_msg_id index returns the first turn_id and discards the
    second enqueue entirely, so a date-only key loses whichever failure came later.

    Exercises the real _report_failure, since the key construction IS the fix.
    """
    pusher._report_failure("diverged", "s", "d")
    pusher._report_failure("timeout", "s", "d")
    pusher._report_failure("diverged", "s", "d")

    assert {path for path, _c, _b in internal_stub.requests} == {"/internal/cron/message"}
    keys = [body["client_msg_id"] for _p, _c, body in internal_stub.requests]
    assert keys[0] != keys[1], "a second kind of failure must not collapse into the first"
    assert keys[0] == keys[2], "same-kind repeats should still dedup to one a day"
    assert "timeout" in keys[1]


def test_a_server_side_refusal_is_not_called_a_divergence(box, pusher, monkeypatch):
    """git prints `! [remote rejected]` when the SERVER declines a push it would
    otherwise accept — a read-only deploy key, say. Matching bare "rejected" would
    tell Mike the remote diverged and send him hunting for commits that don't
    exist."""
    real = pusher._run_git

    def refuse(args, working_dir):
        if "push" in args:
            return subprocess.CompletedProcess(
                args, 1, "",
                "remote: error: refusing to allow a deploy key to push\n"
                "To github.com:mikemason/marlow-identity.git\n"
                " ! [remote rejected] HEAD -> main (pre-receive hook declined)\n"
                "error: failed to push some refs\n",
            )
        return real(args, working_dir)

    monkeypatch.setattr(pusher, "_run_git", refuse)

    assert pusher.main() == 1
    kind, summary, detail = box[2][0]
    assert kind == "remote-refused"
    assert "diverge" not in summary.lower()
    assert "write access" in detail


def test_a_read_only_deploy_key_is_named_as_such(box, pusher, monkeypatch):
    """The most likely first-run failure. GitHub's SSH entry point declines before
    the git protocol starts, so there is no `remote:` prefix to match on — matching
    only the receive-side markers dropped this into the generic "push failed" bucket
    and lost the one hint that resolves it."""
    real = pusher._run_git

    def read_only(args, working_dir):
        if "push" in args:
            return subprocess.CompletedProcess(
                args, 128, "",
                "ERROR: The key you are using has read-only access to this repository.\n"
                "fatal: Could not read from remote repository.\n",
            )
        return real(args, working_dir)

    monkeypatch.setattr(pusher, "_run_git", read_only)

    assert pusher.main() == 1
    kind, summary, detail = box[2][0]
    assert kind == "remote-refused"
    assert "write access" in summary
    assert "Allow write access" in detail


def test_a_failing_git_remote_is_not_called_a_missing_remote(box, pusher, monkeypatch):
    """Empty stdout from a FAILED git reads identically to "no remotes". Telling
    Mike to add an origin that already exists sends him nowhere useful."""
    real = pusher._run_git

    def broken(args, working_dir):
        if args[-1] == "remote":
            return subprocess.CompletedProcess(args, 128, "", "fatal: not a git repository\n")
        return real(args, working_dir)

    monkeypatch.setattr(pusher, "_run_git", broken)

    assert pusher.main() == 1
    assert box[2][0][0] == "git-error"
    assert "no 'origin' remote" not in box[2][0][1]


# --- what the invocation must carry ------------------------------------------


def test_the_push_pins_ssh_to_the_deploy_key(box, pusher, monkeypatch):
    """Correctness rather than a boundary now that this runs as the key's owner: an
    unpinned push offers marlow's default key, which GitHub matches against a
    different repo and declines."""
    captured: list[list[str]] = []
    real_run = subprocess.run

    def spy(args, **kwargs):
        if isinstance(args, list) and "push" in args:
            captured.append(args)
        return real_run(args, **kwargs)

    monkeypatch.setattr(pusher.subprocess, "run", spy)
    assert pusher.main() == 0

    argv = captured[0]
    assert "--no-verify" in argv
    ssh = next(a for a in argv if a.startswith("core.sshCommand="))
    assert "IdentitiesOnly=yes" in ssh
    assert "BatchMode=yes" in ssh
    assert str(pusher.KEY_PATH) in ssh


def test_the_pusher_does_not_touch_root_owned_paths(pusher):
    """The root design is reverted, not half-reverted. A leftover write to /etc or a
    GIT_CONFIG_GLOBAL assignment would mean this script still needs sudo — and
    switching off git's dubious-ownership guard is the thing the reversal was for.

    Checked against the code with the module docstring removed, since that docstring
    names all three deliberately: why the root design existed and why it went is the
    most valuable thing that branch produced, and this test must not pressure anyone
    into deleting it."""
    import ast

    tree = ast.parse(SCRIPT.read_text())
    assert ast.get_docstring(tree), "the module docstring carries the reversal rationale"
    tree.body.pop(0)
    code = ast.unparse(tree)

    for gone in ("/etc/marlow", "GIT_CONFIG_GLOBAL", "safe.directory"):
        assert gone not in code, f"{gone} survived the revert to running as marlow"


def test_the_script_has_no_third_party_imports(pusher):
    """It runs on /usr/bin/python3 from a crontab, with no venv."""
    source = SCRIPT.read_text()
    for banned in ("import httpx", "import fastapi", "import pydantic", "import requests"):
        assert banned not in source
