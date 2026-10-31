"""server/identity_git.py — the harness-owned commit after every turn.

Load-bearing beyond ordinary plumbing: this is the ONE code path that can move a
file out of the working tree without passing file_policy or bash_policy, and
piece 4 of docs/tasks/marlow-working-dir-git-policy.md pushes whatever lands here
to GitHub. Gate #2 found a real `.env` in the working dir holding a live token,
so "a secret never reaches the index" is a security assertion, not hygiene.
"""
from __future__ import annotations

import dataclasses
import shutil
import subprocess

import pytest

from conftest import safe_git_init, write_executable

from server import boundaries, identity_git
from server.identity_git import commit_after_turn

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git required")


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
def repo(tmp_path, monkeypatch):
    """A working dir that is a real git repo, wired in as settings.working_dir."""
    working_dir = tmp_path / "marlow-identity"
    safe_git_init(working_dir, "-b", "main")
    (working_dir / "memory").mkdir(parents=True)
    # Local config, so the test never depends on (or is broken by) the runner's
    # global git identity.
    (working_dir / "SOUL.md").write_text("seed\n")
    _git(working_dir, "add", "-A")
    _git(working_dir, *_ID, "commit", "-q", "-m", "seed: initial identity")

    monkeypatch.setattr(
        identity_git, "settings", dataclasses.replace(identity_git.settings, working_dir=working_dir)
    )
    return working_dir


def _tracked(repo) -> set[str]:
    return set(_git(repo, "ls-files").split())


# --- the no-op cases ---------------------------------------------------------


def test_a_working_dir_that_is_not_a_repo_is_a_silent_no_op(tmp_path, monkeypatch):
    """CI, local dev, and an unseeded box all have one. Must not raise and must
    not be an error — seed-identity.sh is what makes it a repo."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    monkeypatch.setattr(
        identity_git, "settings", dataclasses.replace(identity_git.settings, working_dir=plain)
    )
    assert commit_after_turn("turn-1", "chat") is None


def test_a_clean_tree_produces_no_empty_commit(repo):
    before = _git(repo, "rev-parse", "HEAD")
    assert commit_after_turn("turn-1", "chat") is None
    assert _git(repo, "rev-parse", "HEAD") == before


# --- the ordinary path -------------------------------------------------------


def test_a_memory_write_is_committed(repo):
    (repo / "memory" / "fact.md").write_text("Mike likes banana\n")
    sha = commit_after_turn("turn-1", "chat")
    assert sha
    assert "memory/fact.md" in _tracked(repo)
    assert _git(repo, "status", "--porcelain") == ""


def test_a_deletion_is_committed(repo):
    (repo / "memory" / "fact.md").write_text("x\n")
    commit_after_turn("turn-1", "chat")
    (repo / "memory" / "fact.md").unlink()
    assert commit_after_turn("turn-2", "chat")
    assert "memory/fact.md" not in _tracked(repo)


def test_a_rename_commits_both_halves(repo):
    """The porcelain -z reader has to know a rename record spends an extra field
    on its source path. Getting that wrong desynchronises the whole list, so the
    symptom would be a later path silently going uncommitted."""
    (repo / "memory" / "old.md").write_text("content worth detecting\n" * 5)
    commit_after_turn("turn-1", "chat")
    (repo / "memory" / "new.md").write_text("content worth detecting\n" * 5)
    (repo / "memory" / "old.md").unlink()

    assert commit_after_turn("turn-2", "chat")
    tracked = _tracked(repo)
    assert "memory/new.md" in tracked
    assert "memory/old.md" not in tracked
    assert _git(repo, "status", "--porcelain") == ""


def test_an_untracked_directory_is_picked_up(repo):
    """--untracked-files=all: the default 'normal' mode reports a new directory
    as one entry, which would stage the directory name and miss files under it."""
    (repo / "cron").mkdir()
    (repo / "cron" / "morning.md").write_text("brief\n")
    assert commit_after_turn("turn-1", "cron")
    assert "cron/morning.md" in _tracked(repo)


# --- the security assertion: secrets never reach the index -------------------


def test_a_dotenv_is_never_committed(repo):
    (repo / ".env") .write_text("TRELLO_TOKEN=live-secret\n")
    (repo / "memory" / "fact.md").write_text("ordinary\n")

    assert commit_after_turn("turn-1", "chat")
    tracked = _tracked(repo)
    assert "memory/fact.md" in tracked, "the rest of the turn must still be backed up"
    assert ".env" not in tracked


def test_a_dotenv_variant_is_never_committed(repo):
    """SECRET_GLOBS covers `.env.*` too — gate #2 found .env files in more than
    one shape."""
    (repo / ".env.production").write_text("KEY=x\n")
    (repo / "memory" / "fact.md").write_text("ordinary\n")
    commit_after_turn("turn-1", "chat")
    assert ".env.production" not in _tracked(repo)


def test_a_nested_dotenv_is_never_committed(repo):
    (repo / "scripts").mkdir()
    (repo / "scripts" / ".env").write_text("KEY=x\n")
    (repo / "memory" / "fact.md").write_text("ordinary\n")
    commit_after_turn("turn-1", "chat")
    assert "scripts/.env" not in _tracked(repo)


def test_a_secret_as_the_only_change_commits_nothing(repo):
    before = _git(repo, "rev-parse", "HEAD")
    (repo / ".env").write_text("KEY=x\n")
    assert commit_after_turn("turn-1", "chat") is None
    assert _git(repo, "rev-parse", "HEAD") == before
    assert ".env" not in _tracked(repo)


def test_exclusion_does_not_rest_on_the_working_dir_gitignore(repo):
    """The requirement is that this holds in CODE. The working dir's .gitignore
    is inside the tree the agent can write to, so a test that passed only because
    of a .gitignore would be testing the wrong thing."""
    assert not (repo / ".gitignore").exists()
    (repo / ".env").write_text("KEY=x\n")
    (repo / "memory" / "fact.md").write_text("ordinary\n")
    commit_after_turn("turn-1", "chat")
    assert ".env" not in _tracked(repo)


def test_the_agents_own_harness_config_is_not_committed(repo, monkeypatch):
    """AGENT_CONFIG_PATHS covers <working_dir>/.mcp.json and .claude/ — not
    secrets, but harness rather than identity, and permission-relevant."""
    monkeypatch.setattr(boundaries, "AGENT_CONFIG_PATHS", (str(repo / ".mcp.json"),))
    (repo / ".mcp.json").write_text("{}\n")
    (repo / "memory" / "fact.md").write_text("ordinary\n")

    commit_after_turn("turn-1", "chat")
    tracked = _tracked(repo)
    assert "memory/fact.md" in tracked
    assert ".mcp.json" not in tracked


def test_a_wildcard_filename_stages_only_itself(repo):
    """git pathspecs are wildmatch patterns, not paths. A file literally named
    `*` — creatable by Marlow, since a Write inside memory/ needs no prompt —
    made `git add -- '*'` stage every sibling including excluded ones, which
    tripped the index re-check and would have aborted EVERY later turn's commit
    too (/code-review 2026-08-08). `:(literal)` is the fix."""
    (repo / ".env").write_text("KEY=x\n")
    (repo / "memory" / "*").write_text("wildcard\n")
    (repo / "memory" / "sibling.md").write_text("ordinary\n")

    assert commit_after_turn("turn-1", "chat")
    tracked = _tracked(repo)
    assert "memory/*" in tracked
    assert "memory/sibling.md" in tracked
    assert ".env" not in tracked


def test_gitignored_memory_is_reported_not_silently_dropped(repo, caplog):
    """Change detection is `git status`, which omits ignored paths — and memory/
    is writable with no prompt, so a turn could write memory/.gitignore and opt
    its own memory out of every future commit indistinguishably from a turn that
    changed nothing. The ignore is still honoured; the SILENCE was the hole."""
    (repo / "memory" / ".gitignore").write_text("secret-notes.md\n")
    (repo / "memory" / "secret-notes.md").write_text("not backed up\n")
    (repo / "memory" / "fact.md").write_text("ordinary\n")

    with caplog.at_level("WARNING"):
        assert commit_after_turn("turn-1", "chat")

    tracked = _tracked(repo)
    assert "memory/fact.md" in tracked
    assert "memory/secret-notes.md" not in tracked  # the ignore is respected
    assert "memory/secret-notes.md" in caplog.text  # but it is not silent
    assert "NOT being" in caplog.text


def test_ignored_paths_outside_memory_are_not_warned_about(repo, caplog):
    """Mike's working dir ignores the Trello sync's generated van-board files.
    Warning on those every turn would train the warning to be ignored."""
    (repo / ".gitignore").write_text("van-board.md\n")
    (repo / "van-board.md").write_text("generated\n")
    (repo / "memory" / "fact.md").write_text("ordinary\n")

    with caplog.at_level("WARNING"):
        assert commit_after_turn("turn-1", "chat")
    assert "van-board.md" not in caplog.text


def test_an_ignored_path_is_never_staged(repo):
    """--ignored=matching puts `!!` records in the same status stream. Treating
    one as a change would `add -f`-by-accident the very files the ignore exists
    to keep out."""
    (repo / ".gitignore").write_text("build/\n")
    (repo / "build").mkdir()
    (repo / "build" / "out.o").write_text("junk\n")
    (repo / "memory" / "fact.md").write_text("ordinary\n")

    assert commit_after_turn("turn-1", "chat")
    tracked = _tracked(repo)
    assert not any(path.startswith("build/") for path in tracked)
    # The two real changes (.gitignore and the memory file), and nothing else.
    assert _git(repo, "log", "-1", "--pretty=%s").strip() == "turn: chat turn-1 (2 files)"


# --- properties of the commit itself ----------------------------------------


def test_the_message_carries_no_prompt_text(repo):
    """User content must not land in git history. The
    message is built from the turn id and source, both server-internal."""
    (repo / "memory" / "fact.md").write_text("x\n")
    commit_after_turn("turn-abc123", "chat")
    message = _git(repo, "log", "-1", "--pretty=%s").strip()
    assert message == "turn: chat turn-abc123 (1 file)"


def test_the_message_pluralises(repo):
    (repo / "memory" / "a.md").write_text("x\n")
    (repo / "memory" / "b.md").write_text("y\n")
    commit_after_turn("turn-1", "cron")
    assert _git(repo, "log", "-1", "--pretty=%s").strip() == "turn: cron turn-1 (2 files)"


def test_a_missing_source_still_produces_a_message(repo):
    (repo / "memory" / "a.md").write_text("x\n")
    commit_after_turn("turn-1", None)
    assert _git(repo, "log", "-1", "--pretty=%s").strip() == "turn: turn turn-1 (1 file)"


def test_a_repo_with_no_git_identity_can_still_commit(repo):
    """The seed script hit this first: a fresh system user has no git config and
    a bare `git commit` aborts with 'Author identity unknown'. The -c flags are
    what make this work, so removing them must fail this test.

    The fixture passes its own identity with -c and never writes it to the
    repo config, so this repo already has no configured identity."""
    (repo / "memory" / "fact.md").write_text("x\n")
    assert commit_after_turn("turn-1", "chat")
    assert _git(repo, "log", "-1", "--pretty=%an").strip() == "Marlow"


def test_a_repo_hook_cannot_block_or_hijack_the_commit(repo):
    """--no-verify. `.git/hooks` sits inside the working tree but outside
    WRITABLE_SUBTREE, so writing one PROMPTS rather than being denied — an
    approved hook would otherwise run as this server process on every turn."""
    hook = repo / ".git" / "hooks" / "pre-commit"
    write_executable(hook, "#!/bin/sh\nexit 1\n")
    (repo / "memory" / "fact.md").write_text("x\n")
    assert commit_after_turn("turn-1", "chat")


# --- the repo's own config cannot execute code as this process ---------------
#
# /security-review 2026-08-08 confirmed a full path to RCE as the server process
# (which runs OUTSIDE the bwrap namespace, so it sees the secrets that are tmpfs
# -blanked from the agent): one auto-approved `git log --output=.git/config` wrote
# a config, and the next turn's harness commit executed it. `.git` is now in
# AGENT_CONFIG_PATHS so that write prompts — these are the second layer.


def test_core_fsmonitor_in_the_repo_config_does_not_execute(repo, tmp_path):
    """`git status` runs core.fsmonitor, and status is the FIRST thing this module
    does. --no-verify covers commit hooks and nothing like this."""
    marker = tmp_path / "PWNED_FSMONITOR"
    _git(repo, "config", "core.fsmonitor", f"echo x > {marker}; false")
    _git(repo, "config", "core.fsmonitorHookVersion", "2")
    (repo / "memory" / "fact.md").write_text("x\n")

    assert commit_after_turn("turn-1", "chat")  # still does its job
    assert not marker.exists()


def test_a_clean_filter_in_the_repo_config_refuses_the_commit(repo, tmp_path, caplog):
    """`filter.<n>.clean` runs during `git add` when a .gitattributes assigns it,
    and memory/.gitattributes needs no permission prompt. The key name is
    open-ended, so `-c` cannot neutralise it — hence fail-closed. A missed backup
    is recoverable; executing an agent-chosen command as this process is not."""
    marker = tmp_path / "PWNED_CLEAN"
    _git(repo, "config", "filter.ev.clean", f"echo x > {marker}; cat")
    (repo / "memory" / ".gitattributes").write_text("fact.md filter=ev\n")
    (repo / "memory" / "fact.md").write_text("x\n")

    with caplog.at_level("ERROR"):
        assert commit_after_turn("turn-1", "chat") is None

    assert not marker.exists()
    assert "filter.ev.clean" in caplog.text
    assert "REFUSING" in caplog.text
    assert "memory/fact.md" not in _tracked(repo)


def test_an_include_directive_refuses_the_commit(repo, tmp_path):
    """An `include`/`includeIf` merges in a file that can define a filter, and
    the included keys do not show up under `git config --local`. Covering the
    directive itself is what catches that."""
    evil = tmp_path / "evil.gitconfig"
    evil.write_text('[filter "ev"]\n\tclean = echo pwned\n')
    _git(repo, "config", "include.path", str(evil))
    (repo / "memory" / "fact.md").write_text("x\n")

    assert commit_after_turn("turn-1", "chat") is None


def test_ordinary_local_config_is_not_mistaken_for_tampering(repo):
    """The real box's identity repo has a committer identity and (piece 4) an
    origin. Neither may stop the backups."""
    _git(repo, "config", "remote.origin.url", "git@github.com:mikemason/marlow-identity.git")
    _git(repo, "config", "branch.main.remote", "origin")
    _git(repo, "config", "core.sshCommand", "ssh -i /root/keys/id_ed25519")
    (repo / "memory" / "fact.md").write_text("x\n")

    assert commit_after_turn("turn-1", "chat")


def test_an_unreadable_config_raises_instead_of_reporting_clean(repo, monkeypatch):
    """The fail-open that piece 4 would have walked into.

    `git config` exits 1 for "no matching keys" but 128 for "detected dubious
    ownership" — which is what root gets in marlow's repo. Read as "no keys", a 128
    reports a clean repo without having read anything, defeating the check that
    exists to stop root executing an agent-chosen command.
    """
    real_run = identity_git.subprocess.run

    def dubious(args, **kwargs):
        if "config" in args:
            return subprocess.CompletedProcess(
                args, 128, "", "fatal: detected dubious ownership in repository\n"
            )
        return real_run(args, **kwargs)

    monkeypatch.setattr(identity_git.subprocess, "run", dubious)

    with pytest.raises(identity_git.GitConfigUnreadable):
        identity_git.tampered_config_keys(repo)


def test_an_unreadable_config_refuses_the_commit(repo, monkeypatch, caplog):
    """Fail-closed at the call site too: could-not-check is not the same as clean,
    so nothing is staged."""
    def unreadable(*args, **kwargs):
        raise identity_git.GitConfigUnreadable("exited 128")

    monkeypatch.setattr(identity_git, "tampered_config_keys", unreadable)
    (repo / "memory" / "fact.md").write_text("x\n")

    assert commit_after_turn("turn-1", "chat") is None
    assert "REFUSING" in caplog.text
    assert "fact.md" not in _tracked(repo)


def test_no_local_config_keys_is_still_not_tampering(repo, monkeypatch):
    """git's exit 1 must keep meaning "nothing matched", or every commit stops."""
    real_run = identity_git.subprocess.run

    def no_keys(args, **kwargs):
        if "config" in args:
            return subprocess.CompletedProcess(args, 1, "", "")
        return real_run(args, **kwargs)

    monkeypatch.setattr(identity_git.subprocess, "run", no_keys)
    assert identity_git.tampered_config_keys(repo) == []


def test_a_git_failure_is_swallowed_not_raised(repo, monkeypatch):
    """A turn's reply is already the user's by the time this runs. Durability
    must never be able to break a conversation."""
    def boom(*args, **kwargs):
        raise OSError("git is gone")

    monkeypatch.setattr(identity_git.subprocess, "run", boom)
    (repo / "memory" / "fact.md").write_text("x\n")
    assert commit_after_turn("turn-1", "chat") is None
