"""The Bash allowlist (server/bash_policy.py).

Two properties, tested separately because they carry different weight:

  * a hard deny must hold for anything that names a secret path, however the
    command dresses it up -- this is the /security-review 2026-08-05 finding and
    the tests below are the regression suite for it;
  * a vetted-allow must be *conservative*. Every miss costs a phone prompt and
    nothing more, so the tests assert PROMPT generously and ALLOW narrowly.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FAKE_HOME, FAKE_TREE
from server import boundaries
from server.boundaries import (
    VETTED_GIT_SUBCOMMAND_OPTS,
    VETTED_GIT_SUBCOMMANDS,
)
from server.boundaries import is_agent_config_path
from server.bash_policy import (
    ALLOW,
    command_touches_harness_only_path,
    PROMPT,
    command_is_git_write,
    command_touches_agent_config,
    command_touches_secret_path,
    vet_bash_command,
)

TREE = FAKE_TREE


# --- the hard deny: secret paths inside a Bash argument ---------------------

# Built from the live config rather than written as box-path literals:
# SECRET_PATHS resolves $HOME and the MARLOW_* vars at import, so a hardcoded
# box path passes on the box and silently tests nothing anywhere else.
def _secret() -> tuple[str, str]:
    from server.config import settings

    return str(settings.credentials_path), str(settings.data_dir / "device_tokens.json")


@pytest.mark.parametrize("template", [
    # The finding itself: prefix `git diff` matched settings.json's allow rule.
    "git diff --no-index {path} /dev/null",
    "cat {path}",
    "wc -c {path}",
    "ls {path}",
    # The value hides behind an `=`, so a whitespace scan alone would miss it.
    "git diff --output={path} HEAD",
    # Quoted, so a naive split() keeps the quotes attached to the token and the
    # secret path no longer matches.
    "cat '{path}'",
])
def test_secret_paths_are_caught_in_bash_arguments(template) -> None:
    for path in (*_secret(), "~/.ssh/id_ed25519"):
        assert command_touches_secret_path(template.format(path=path)), template


def test_unparseable_commands_are_still_scanned() -> None:
    """An unbalanced quote makes shlex give up. Falling back to whitespace
    splitting matters: the alternative is that adding a stray quote turns the
    hard deny off."""
    assert command_touches_secret_path("cat ~/.ssh/id_ed25519 'oops")


@pytest.mark.parametrize("command", [
    "git status",
    "git log --oneline -20",
    "ls -la memory/",
    "date",
])
def test_ordinary_commands_touch_no_secret(command) -> None:
    assert not command_touches_secret_path(command)


# --- the vetted allow ------------------------------------------------------

@pytest.mark.parametrize("command", [
    "git status",
    "git status --short",
    "git log --oneline -20",
    "git log --format=%h",
    "git diff HEAD",
    "git show HEAD:memory/notes.md",
    "git branch",
    "git --no-pager log -1",
    "git blame memory/notes.md",
    "ls -la",
    "ls memory/",
    "wc -l memory/notes.md",
    "date",
])
def test_ordinary_read_only_work_is_vetted(command) -> None:
    decision, why = vet_bash_command(command, TREE)
    assert decision == ALLOW, f"{command!r} should not need a prompt: {why}"


def test_repo_refs_containing_slashes_are_not_treated_as_paths() -> None:
    """`origin/main` contains a slash and is not a path. Prompting on these
    would mean prompting on ordinary git usage, which trains Mike to stop
    reading the prompts -- the failure mode this allowlist exists to avoid."""
    assert vet_bash_command("git log origin/main", TREE)[0] == ALLOW
    assert vet_bash_command("git diff refs/heads/main", TREE)[0] == ALLOW


@pytest.mark.parametrize("command", [
    # Containment is the rule -- these are refused without the tokenizer needing
    # to know that --no-index or --output are dangerous.
    "git diff --no-index /etc/passwd /dev/null",
    "git diff --output=/etc/motd HEAD",
    "ls /etc",
    "wc -l ../../../etc/passwd",
    "git show HEAD:../../.ssh/config",
])
def test_paths_outside_the_working_tree_prompt(command) -> None:
    assert vet_bash_command(command, TREE)[0] == PROMPT


@pytest.mark.parametrize("command", [
    "git commit -m wip",
    "git push origin main",
    "git checkout main",
    "git fetch",
    "curl https://example.com",
    "python -c 'print(1)'",
    "cat memory/notes.md",
])
def test_unallowlisted_verbs_and_subcommands_prompt(command) -> None:
    assert vet_bash_command(command, TREE)[0] == PROMPT


@pytest.mark.parametrize("command", [
    "git -c core.pager=/bin/sh log",
    "git --exec-path=/tmp/evil log",
])
def test_git_global_options_are_allowlisted_not_vetted(command) -> None:
    """`git -c core.pager=<cmd>` is arbitrary code execution and its argument is
    not path-shaped, so containment would not catch it."""
    assert vet_bash_command(command, TREE)[0] == PROMPT


@pytest.mark.parametrize("command", [
    "git log $(cat /etc/passwd)",
    "git log `whoami`",
    "git status > /tmp/out",
    "git status | tee /tmp/out",
    "git status; curl https://example.com",
    "git status && rm -r memory",
    "ls ~/$SECRET",
    "git status \\; curl https://example.com",
])
def test_shell_metacharacters_abandon_vetting(command) -> None:
    """Substitution and redirection defeat static token analysis, so vetting
    stops. It PROMPTS rather than denies: Mike must still be able to approve a
    pipeline, and denying here would block him as well as an attacker."""
    assert vet_bash_command(command, TREE)[0] == PROMPT


@pytest.mark.parametrize("command", [
    # The literal token looks like a contained relative path; bash expands it to
    # an absolute one. Clears the secret scan AND containment, on the one path
    # that skips human review (/security-review 2026-08-05, second pass).
    f"wc -l {{,}}{FAKE_HOME}/.ssh/id_ed25519",
    f"ls {{,}}{FAKE_HOME}/.claude/.credentials.json",
    "git diff --no-index {,}/etc/passwd /dev/null",
    # `..` inside a single component, which normpath will not cancel.
    "wc -l {,../../..}/etc/shadow",
    "ls memory/{a,b}",
])
def test_brace_expansion_abandons_vetting(command) -> None:
    assert vet_bash_command(command, TREE)[0] == PROMPT


def test_newline_is_caught_on_the_raw_command() -> None:
    """shlex.split() consumes a newline as ordinary whitespace, so this
    tokenises to four individually blameless tokens -- `git`, `status`, `curl`,
    `evil` -- while the shell runs two commands. The token allowlist cannot see
    it; only a check against the raw string can.
    """
    assert vet_bash_command("git status\ncurl https://evil.example", TREE)[0] == PROMPT


def test_quoted_metacharacters_still_abandon_vetting() -> None:
    """shlex strips the quotes, so the token reaching the allowlist is bare.
    Quoting makes these inert to the shell, so this is conservative rather than
    necessary -- and conservative costs one prompt."""
    assert vet_bash_command("git log '$(whoami)'", TREE)[0] == PROMPT
    assert vet_bash_command('git log "|foo"', TREE)[0] == PROMPT


@pytest.mark.parametrize("command", [
    "git diff HEAD^",
    "git log --format=%h",
    "git log --pretty=format:%h",
    "date +%Y-%m-%d",
    "git diff HEAD~3",
    "wc -l memory/2026-08-05_notes.md",
])
def test_the_allowlist_does_not_prompt_on_ordinary_punctuation(command) -> None:
    """The charset has to carry real usage or it trains Mike to stop reading
    prompts -- the failure mode this whole allowlist exists to avoid."""
    decision, why = vet_bash_command(command, TREE)
    assert decision == ALLOW, f"{command!r} should not need a prompt: {why}"


def test_unbalanced_quoting_prompts() -> None:
    assert vet_bash_command("git log 'unclosed", TREE)[0] == PROMPT


@pytest.mark.parametrize("command", ["wc -l *", "ls */*", "wc -l memory/?.md"])
def test_globs_are_not_vetted(command) -> None:
    """Containment resolves the literal token. When the token is a glob, the
    paths the shell opens are never resolved -- `wc -l *` clears as
    `<tree>/*`, then expands to include any symlink pointing out. The realpath
    half of containment only ever protected literal paths."""
    assert vet_bash_command(command, TREE)[0] == PROMPT


def test_unresolvable_tilde_user_is_not_vetted() -> None:
    """Python 3.13+ raises RuntimeError from expanduser() for a `~user` it
    cannot resolve. Uncontainable, so it must not be vetted -- and it must not
    raise: an unhandled exception kills the hook mid-check."""
    assert vet_bash_command("wc -l ~nosuchuser/x", TREE)[0] == PROMPT


def test_unresolvable_tilde_user_does_not_break_the_secret_scan() -> None:
    """The crash landed inside command_touches_secret_path, before the secret
    token was reached -- so a junk `~user` prefix disabled the hard deny."""
    from server.config import settings

    assert command_touches_secret_path(f"cat ~nosuchuser/x {settings.credentials_path}")


def test_symlink_out_of_the_tree_is_not_vetted(tmp_path) -> None:
    """A symlink inside the working tree pointing at ~/.ssh is a plausible thing
    to find in a home directory, and normalising the path alone would call it
    contained."""
    tree = tmp_path / "tree"
    tree.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("x")
    (tree / "escape").symlink_to(outside)

    assert vet_bash_command("wc -l escape/secret", str(tree))[0] == PROMPT
    assert vet_bash_command("wc -l ./escape/secret", str(tree))[0] == PROMPT


class TestProtectedPathsInsideTheTree:
    """/security-review 2026-08-06 -- instance seven of docs/tasks/resolve-paths-once.md.

    Containment is not sufficient on its own, because the working tree CONTAINS
    protected files: gate #2 found a real .env in the working dir, and
    AGENT_CONFIG_PATHS deliberately covers <working_dir>/.mcp.json and .claude/.

    A RELATIVE argument defeated is_secret_path in two ways at once -- its
    SECRET_GLOBS half is an fnmatch and `**/.env` needs a separator, so a bare
    `.env` matched nothing; its SECRET_PATHS half realpaths, but an unanchored
    relative path realpaths against the hook process's cwd, not the tree the
    tool acts in. The CLI runs with cwd=working_dir, so relative is the normal
    form.
    """

    @pytest.fixture
    def tree(self, tmp_path):
        t = tmp_path / "tree"
        (t / "memory").mkdir(parents=True)
        (t / ".claude").mkdir()
        (t / ".env").write_text("DISCORD_TOKEN=live\n")
        (t / "memory" / "projects.md").write_text("notes\n")
        return t

    @pytest.mark.parametrize("command", [
        "wc -l .env",
        "wc -l ./.env",
        "wc -l memory/../.env",
    ])
    def test_a_relative_secret_is_not_vetted(self, command, tree) -> None:
        """SECRET_GLOBS is path-independent, so a scratch tree tests it truly."""
        assert vet_bash_command(command, working_tree=str(tree))[0] == PROMPT

    @pytest.mark.parametrize("command", ["ls .claude", "wc -c .mcp.json"])
    def test_a_relative_agent_config_path_is_not_vetted(self, command) -> None:
        """Against the LIVE working_dir, not a scratch tree.

        AGENT_CONFIG_PATHS is built from settings.working_dir at import, so a
        tmp_path tree's `.claude` is not one of them and the assertion would
        pass for the wrong reason on the box and fail everywhere else -- the
        same trap as the `_secret` helper above, and the fourth time it has
        bitten during this work.
        """
        from server.config import settings

        assert vet_bash_command(command, working_tree=str(settings.working_dir))[0] == PROMPT

    @pytest.mark.parametrize("command", [
        # The exploit /security-review found on 2026-08-08: `--output` on a
        # vetted READ subcommand writes arbitrary bytes anywhere containment
        # allows, and `%x##` synthesizes the `[`/`]`/newlines a git config needs
        # without one character outside VETTED_TOKEN_CHARS. Landing that on
        # .git/config gave `core.fsmonitor`/`filter.<n>.clean` -- commands the
        # harness's own post-turn `git status`/`git add` then execute as the
        # SERVER process, outside the bwrap namespace. It vetted ALLOW, so no
        # human ever saw it.
        "git log -1 --format=%x5Bcore%x5D --output=.git/config",
        "git diff --output=.git/info/attributes HEAD",
        "git show --output=.git/hooks/pre-commit HEAD",
        "wc -l .git/config",
    ])
    def test_the_identity_repos_own_metadata_is_not_vetted(self, command) -> None:
        """Against the LIVE working_dir, for the reason on the test above."""
        from server.config import settings

        assert vet_bash_command(command, working_tree=str(settings.working_dir))[0] == PROMPT

    @pytest.mark.parametrize("command", [
        "git status",
        "ls memory",
        "wc -l memory/projects.md",
        "git log --oneline -5",
    ])
    def test_ordinary_in_tree_work_is_still_vetted(self, command, tree) -> None:
        """The fix must not turn every relative argument into a prompt -- that
        would reinstate the five-prompt turn this branch exists to remove."""
        assert vet_bash_command(command, working_tree=str(tree))[0] == ALLOW

    def test_prompt_not_deny(self, tree) -> None:
        """vet_bash_command's contract is ALLOW-or-PROMPT. A hard deny here
        would be unrecoverable, and any token that merely LOOKS like `.env` --
        a git --grep argument, a message body -- would earn one."""
        assert vet_bash_command("wc -l .env", working_tree=str(tree)) == (
            PROMPT, "argument reaches a secret or agent-config path: .env"
        )


def test_grep_is_not_a_vetted_command() -> None:
    """Added 2026-08-06 and removed the same day by /security-review.

    Every other verb on the list returns names (ls), counts (wc) or repo
    metadata (git). grep returns file CONTENTS, and containment cannot bound it:
    `grep -r . .` names no target at all, so the token scan has nothing to
    inspect while the command reads every file in a tree that holds a live .env.
    """
    assert "grep" not in boundaries.VETTED_COMMANDS
    assert vet_bash_command("grep -r . .")[0] == PROMPT
    assert vet_bash_command("grep TOKEN memory/projects.md")[0] == PROMPT


class TestAgentConfigIsNotWritable:
    """Since --strict-mcp-config came out of invoke.py (2026-08-06), the CLI
    honours every MCP config source. An mcpServers entry is a command that runs
    at CLI startup, before any tool call -- so the hook never sees it, and bwrap
    does not stop it (the network is deliberately not unshared). These files
    being untouchable is what stands where the flag stood."""

    @pytest.mark.parametrize("command", [
        "ls ~/.claude.json",
        "wc -l ~/.claude/settings.json",
        "git diff ~/.claude/settings.local.json",
        "ls ~/.claude/plugins",
    ])
    def test_config_paths_are_flagged(self, command):
        assert command_touches_agent_config(command)

    def test_a_similarly_named_sibling_is_not_flagged(self):
        """`~/.claude.jsonbak` shares a prefix with ~/.claude.json but is a
        different file. Matching on raw string prefix would flag it, and worse,
        would flag ~/.claude-notes/ too."""
        assert not command_touches_agent_config("ls ~/.claude.jsonbak")

    def test_the_option_value_form_is_caught(self):
        """`--output=~/.claude.json` hides the path from a naive token scan --
        the same shape the secret check already handles."""
        assert command_touches_agent_config("git log --output=~/.claude.json")

    def test_ordinary_commands_are_not_flagged(self):
        assert not command_touches_agent_config("git status")
        assert not command_touches_agent_config("ls memory")


class TestPathTraversalAgainstTheConfigDeny:
    """/security-review 2026-08-06 found the deny was string-prefix
    only. expanduser() collapses `.` but deliberately leaves `..` alone -- it
    can cross a symlink -- so `~/.claude/../.claude.json` named the protected
    file while clearing the check, downgrading a HARD deny (refused even if Mike
    would approve) to an ordinary phone prompt. On approval that writes an
    mcpServers entry whose command runs at the next CLI startup: before any tool
    call, so outside both the hook and the bwrap namespace."""

    @pytest.mark.parametrize("disguised", [
        "~/.claude/../.claude.json",
        "~/.claude/x/../settings.json",
        "~/.ssh/../.claude.json",
        "~/.claude/./settings.json",
    ])
    def test_dotdot_no_longer_disguises_a_protected_path(self, disguised):
        assert is_agent_config_path(disguised)

    def test_a_symlink_into_the_config_is_caught(self, tmp_path, monkeypatch):
        """The version with no `..` visible at all: a link inside the working
        tree, whose Edit rule is on the unattended allow list, pointing out at
        ~/.claude.json. Prefix matching cannot see this; realpath can."""
        config = tmp_path / ".claude.json"
        config.write_text("{}")
        link = tmp_path / "tree" / "notes.md"
        link.parent.mkdir()
        link.symlink_to(config)
        monkeypatch.setattr(boundaries, "AGENT_CONFIG_PATHS", (str(config),))

        assert is_agent_config_path(str(link))

    def test_a_sibling_sharing_a_prefix_is_still_allowed(self):
        """The fix must not become an overreach: these resolve nowhere near the
        protected files and denying them would cost real prompts."""
        assert not is_agent_config_path("~/.claude.jsonbak")
        assert not is_agent_config_path("~/.claude-notes/today.md")

    def test_a_root_of_slash_would_not_swallow_the_filesystem(self, monkeypatch):
        """Guard on the guard: normpath("/") is "/", and "/".rstrip("/") + "/"
        is "/", which every absolute path starts with. A stray "/" entry would
        silently deny every tool call on the box."""
        monkeypatch.setattr(boundaries, "AGENT_CONFIG_PATHS", ("/",))
        assert not is_agent_config_path(f"{FAKE_TREE}/memory/x.md")


class TestGitWriteIsHarnessOwned:
    """`command_is_git_write` is a HARD deny: git in the identity dir belongs to
    server/identity_git.py, not to the model. Reads stay approvable, because
    reviewing the history of its own memory is real work for a persistent-memory
    assistant."""

    def test_write_subcommands_are_denied(self):
        for command in (
            "git commit -m 'note'",
            "git add memory/fact.md",
            "git push origin main",
            "git reset --hard",
            "git rebase -i HEAD~2",
            "git clean -fd",
            "git rm memory/old.md",
            "git mv a.md b.md",
        ):
            assert command_is_git_write(command), command

    def test_read_subcommands_are_not_denied(self):
        for command in (
            "git status",
            "git log --oneline -5",
            "git diff HEAD~1",
            "git show HEAD:memory/fact.md",
            "git blame memory/fact.md",
            "git branch",
            "git rev-parse HEAD",
        ):
            assert not command_is_git_write(command), command

    def test_subcommands_nobody_enumerated_are_denied_by_default(self):
        """The whole point of inverting the allowlist rather than listing write
        verbs. A hand-written denylist omits these; this module has already been
        caught twice by a denylist that omitted a case."""
        for command in (
            "git stash",
            "git apply patch.diff",
            "git cherry-pick abc123",
            "git worktree add /tmp/wt",
            "git filter-branch --all",
            "git submodule update",
        ):
            assert command_is_git_write(command), command

    def test_global_options_do_not_hide_the_subcommand(self):
        assert command_is_git_write("git -c core.pager=cat commit -m x")
        assert command_is_git_write("git --no-pager add .")
        assert not command_is_git_write("git --no-pager log")

    def test_a_compound_command_cannot_smuggle_a_write(self):
        """`ls && git commit` tokenises with `ls` in the verb slot, so a
        verb-position-only check would miss it and leave the call merely
        promptable."""
        assert command_is_git_write("ls && git commit -m x")
        assert command_is_git_write("date; git push")
        assert command_is_git_write("cd memory && git add -A")

    def test_an_absolute_git_path_cannot_walk_around_it(self):
        assert command_is_git_write("/usr/bin/git commit -m x")
        assert not command_is_git_write("/usr/bin/git status")

    def test_bare_git_and_version_queries_are_not_writes(self):
        """No subcommand at all is nothing to deny -- and denying `git --version`
        would be an overreach with no upside."""
        assert not command_is_git_write("git")
        assert not command_is_git_write("git --version")

    def test_a_non_git_command_is_never_a_git_write(self):
        assert not command_is_git_write("ls -la memory")
        assert not command_is_git_write("wc -l memory/fact.md")

    def test_the_word_git_as_an_ARGUMENT_is_not_a_write(self):
        """A hard deny never reaches the phone, so a false positive here is
        unapprovable. The first version scanned every `git` token and so denied
        `grep -n git notes.md` outright (/code-review 2026-08-08) — searching its
        own memory for the word is ordinary work."""
        for command in (
            "grep -n git memory/notes.md",
            "grep -rn 'git worktree' memory/",
            "echo git things",
            "wc -l git",
        ):
            assert not command_is_git_write(command), command

    def test_nothing_is_lost_by_only_scanning_command_position(self):
        """The auto-approve path requires `git` in the verb slot
        (VETTED_COMMANDS), so a `git` token elsewhere can only ever be reaching a
        prompt. Wrappers that run a command as their argument are still caught."""
        assert command_is_git_write("env git commit -m x")
        assert command_is_git_write("xargs git reset --hard")
        assert command_is_git_write("nohup git push origin main")
        # And a command whose verb is unvetted still cannot be auto-approved.
        assert vet_bash_command("grep -n git memory/notes.md", TREE)[0] == PROMPT

    def test_a_global_option_with_a_separate_value_does_not_fake_a_write(self):
        """`-C dir` is two tokens; stripping only the flag left `dir` in the
        subcommand slot, so every `git -C memory log` — a read — was hard-denied
        (/code-review 2026-08-08)."""
        for command in (
            "git -C memory log --oneline",
            "git -c core.pager=cat log",
            "git --git-dir .git status",
            "git --work-tree . diff HEAD~1",
        ):
            assert not command_is_git_write(command), command

    def test_a_separate_value_still_does_not_hide_a_write(self):
        for command in (
            "git -C memory commit -m x",
            "git -c user.name=x commit -m x",
            "git --git-dir .git push",
        ):
            assert command_is_git_write(command), command

    def test_an_unknown_global_option_falls_to_deny_not_allow(self):
        """If git grows an option that takes a value, the value lands in the
        subcommand slot and is not on the read allowlist — so this denies. The
        cost is a prompt-shaped read becoming a deny; the alternative shape would
        let a write through."""
        assert command_is_git_write("git --future-option value commit -m x")
        assert command_is_git_write("git --future-option value log")


# --- git options: a read verb must not be a write primitive ----------------
#
# Found 2026-08-28 (docs/tasks/vetted-git-write-primitive.md). `git log`,
# `git diff` and `git show` are vetted as READS, and all three take
# `--output=<file>`; `--format=%x..` supplies arbitrary bytes. Containment was
# the only test applied, so a path INSIDE the working tree was auto-approved --
# an unprompted arbitrary write, through a verb vetted for reading, bypassing
# file_policy's `memory/`-only write rule entirely.

@pytest.mark.parametrize("command", [
    "git log --output=notes.md --format=%h",
    "git log --output=memory/notes.md",          # memory/ does not make a write a read
    "git log --output=SOUL.md",                  # the file file_policy protects hardest
    "git log --output notes.md",                 # the space form, not just `=`
    "git show --output=notes.md HEAD",
    "git diff --output=notes.md HEAD",
])
def test_git_output_is_a_write_and_must_not_be_auto_approved(command) -> None:
    decision, why = vet_bash_command(command, TREE)
    assert decision == PROMPT, f"{command!r} is a write and must reach Mike: {why}"


def test_unrecognised_git_options_prompt_rather_than_deny() -> None:
    """The allowlist is a convenience boundary. A missing entry costs a prompt,
    which is visible and cheap to widen -- never a hard deny, which would block
    Mike too and cannot be answered from the phone."""
    decision, why = vet_bash_command("git log --some-new-flag", TREE)
    assert decision == PROMPT
    assert "option" in why


@pytest.mark.parametrize("command", [
    "git log --oneline -20",
    "git log -5",
    "git log -n 5 --format=%h",
    "git log --stat --no-color",
    "git log --since=2026-08-01 --until=2026-08-28 --author=marlow",
    "git log --name-only --reverse --follow memory/notes.md",
    "git diff --stat HEAD",
    "git show --name-status HEAD",
    "git log --graph --decorate --abbrev-commit",
    "git log --pretty=format:%h -- memory/notes.md",
])
def test_real_read_only_git_usage_still_needs_no_prompt(command) -> None:
    """Reviewing the history of its own memory is ordinary work for a
    persistent-memory assistant. If this list prompts, the allowlist is too
    tight and Mike learns to thumb Allow without reading."""
    decision, why = vet_bash_command(command, TREE)
    assert decision == ALLOW, f"{command!r} should not need a prompt: {why}"


def test_no_index_stays_off_the_allowlist() -> None:
    """`git diff --no-index` is the /security-review 2026-08-05 finding: it made
    a prefix-matched `Bash(git diff:*)` allow read ~/.ssh. Containment catches
    the out-of-tree case, but the option earns no entry here regardless."""
    assert vet_bash_command("git diff --no-index a b", TREE)[0] == PROMPT


# --- options are per-subcommand, not a flat set ----------------------------
#
# /code-review medium 2026-08-28 found three holes in the first version of
# VETTED_GIT_SUBCOMMAND_OPTS, all the same mistake: one flat set, but a short
# option means different things to different subcommands. Each of these was
# ALLOW before the set was keyed by subcommand.

@pytest.mark.parametrize("command,why", [
    ("git branch -u origin/main", "-u is --set-upstream-to here, and WRITES .git/config"),
    ("git branch -M newname", "-M is --move --force here, a ref rename"),
    ("git branch -m newname", "the unforced rename spelling"),
    ("git branch -d old", "deletion"),
    ("git branch --edit-description", "opens an editor and writes the config"),
])
def test_git_branch_write_options_are_not_auto_approved(command, why) -> None:
    decision, reason = vet_bash_command(command, TREE)
    assert decision == PROMPT, f"{command!r} ({why}) must reach Mike: {reason}"


def test_the_short_and_long_spellings_of_a_write_agree() -> None:
    """The tell that the flat set was wrong: `--set-upstream-to=` prompted while
    its own shorthand `-u` was allowed. Same option, opposite decisions."""
    assert vet_bash_command("git branch --set-upstream-to=origin/main", TREE)[0] == PROMPT
    assert vet_bash_command("git branch -u origin/main", TREE)[0] == PROMPT


def test_the_same_short_option_can_be_safe_elsewhere() -> None:
    """`-u` on `status` is `--untracked-files`, a read. Keying by subcommand is
    what lets one be allowed and the other not -- a flat denylist of `-u` would
    have cost this."""
    assert vet_bash_command("git status -u", TREE)[0] == ALLOW
    assert vet_bash_command("git diff -M HEAD", TREE)[0] == ALLOW


@pytest.mark.parametrize("command", ["git tag v9", "git tag -a v9", "git tag --list"])
def test_git_tag_is_a_write_verb_and_is_denied(command) -> None:
    """`git tag <name>` writes a ref with no options at all, so the option
    allowlist cannot reach it. Removing `tag` from VETTED_GIT_SUBCOMMANDS makes
    it a HARD DENY, because command_is_git_write inverts that set -- including
    `--list`, which is the accepted cost."""
    assert command_is_git_write(command)


def test_every_vetted_subcommand_has_an_option_policy() -> None:
    """Adding a subcommand to VETTED_GIT_SUBCOMMANDS without deciding which of
    its options are reads is how `-u` got in. This makes that omission fail
    here rather than in production."""
    assert set(VETTED_GIT_SUBCOMMAND_OPTS) == set(VETTED_GIT_SUBCOMMANDS)


@pytest.mark.parametrize("command", [
    "git status -sb",        # the common bundled spelling
    "git log -n5",           # attached value
    "git diff -U3 HEAD",
    "git shortlog -sne",
])
def test_bundled_and_attached_short_options_do_not_prompt(command) -> None:
    """git accepts these spellings and Marlow uses them. Matching the token
    whole prompted on ordinary reads, which is the failure mode this allowlist
    exists to avoid."""
    decision, why = vet_bash_command(command, TREE)
    assert decision == ALLOW, f"{command!r} should not need a prompt: {why}"


def test_a_bundle_cannot_launder_an_unvetted_flag() -> None:
    """Each constituent is checked separately, so bundling an allowed flag with
    a disallowed one does not clear the pair."""
    assert vet_bash_command("git branch -va", TREE)[0] == ALLOW
    assert vet_bash_command("git branch -vu origin/main", TREE)[0] == PROMPT


@pytest.mark.parametrize("command", [
    # Each of these was ALLOW until 2026-08-28, and each PROMPTs without the
    # `--`. git swallows the separate-token `--` as the value of the preceding
    # allowlisted option (`--until`, `--format`, `-L`) and then resumes option
    # parsing, so a policy that stopped scanning at `--` stopped early while git
    # did not. Confirmed against git 2.50.1, effects and all.
    "git shortlog --until -- --output=memory/notes.md --format=%x41 HEAD",
    "git log -L -- --output=SOUL.md",
    "git blame -L -- --output=SOUL.md a.txt",
    "git branch --format -- -m main pwned",
    "git branch --format -- -D main",
    "git branch --format -- -u origin/main main",
    "git branch --sort -- -m main pwned",
])
def test_a_double_dash_does_not_end_option_scanning(command) -> None:
    """/security-review 2026-08-28. The confirmed effects were an arbitrary
    in-tree write, truncation of SOUL.md, a ref rename, a ref delete, and a
    `[branch]` section written to .git/config -- all auto-approved, no prompt."""
    decision, why = vet_bash_command(command, TREE)
    assert decision == PROMPT, f"{command!r} must not auto-approve: {why}"


def test_a_pathspec_after_a_double_dash_still_reads_normally() -> None:
    """The cost of never breaking is a pathspec that looks like an option, which
    is one prompt. Ordinary pathspecs are unaffected."""
    assert vet_bash_command("git log --oneline -- memory/notes.md", TREE)[0] == ALLOW


class TestHarnessOnlyPaths:
    """The cron spool is the harness's, and the agent must not reach it.

    Backstop for the bwrap tmpfs, which is the real boundary. These matter when
    confinement is off (dev boxes, this suite) -- exactly where the unit's
    `SupplementaryGroups=crontab` would otherwise be unopposed
    (/security-review 2026-08-30).
    """

    @pytest.mark.parametrize(
        "path",
        [
            "/var/spool/cron",
            "/var/spool/cron/crontabs",
            "/var/spool/cron/crontabs/marlow",
            "/var/spool/cron/../cron/crontabs/marlow",
        ],
    )
    def test_the_spool_is_harness_only(self, path) -> None:
        assert boundaries.is_harness_only_path(path)

    @pytest.mark.parametrize("path", ["/var/spool", "/var/spoolcron", "/var/log/cron"])
    def test_neighbouring_paths_are_not_swept_in(self, path) -> None:
        assert not boundaries.is_harness_only_path(path)

    @pytest.mark.parametrize(
        "command",
        [
            "cat /var/spool/cron/crontabs/marlow",
            "git log --output=/var/spool/cron/crontabs/marlow",
            "ls /var/spool/cron/../cron/crontabs",
        ],
    )
    def test_a_command_reaching_the_spool_is_flagged(self, command) -> None:
        assert command_touches_harness_only_path(command)

    def test_an_ordinary_command_is_not(self) -> None:
        assert not command_touches_harness_only_path("git log --oneline -n 5")


# --- content verbs: cleared by NAMED TARGETS, not by containment ------------
#
# Added 2026-09-06. These need a REAL directory, unlike every test above:
# the rule's load-bearing clause is "every target is an existing regular file",
# so FAKE_TREE -- a path that does not exist -- would score every command as a
# prompt and the suite would pass while testing nothing.


@pytest.fixture
def content_tree():
    """A working tree with the shapes the rule has to tell apart.

    Built inside the CONFIGURED working dir rather than a bare tmp_path, and
    that is not incidental. `boundaries.AGENT_CONFIG_PATHS` is computed from
    MARLOW_WORKING_DIR at import, so `<tmp_path>/.claude/settings.json` is not
    agent config to `is_agent_config_path` no matter what tree is passed
    alongside it -- a fixture that ignored this scored `cat .claude/settings.json`
    as ALLOW and would have asserted a hole was a feature. Production has the
    two agreeing because the tree IS the working dir; the test has to as well.
    """
    from server.config import settings

    tree = Path(settings.working_dir)
    created = []
    for relative, body in (
        ("memory/travel.md", "Cabot Trail\n"),
        ("memory/health.md", "physio 9pm\n"),
        ("notes.md", "x\n"),
        (".env", "SECRET=1\n"),
        (".claude/settings.json", "{}\n"),
    ):
        target = tree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
        created.append(target)
    outside = tree.parent / "outside.md"
    outside.write_text("out\n")
    created.append(outside)
    yield str(tree)
    for target in created:
        target.unlink(missing_ok=True)


@pytest.mark.parametrize("command", [
    "cat memory/travel.md",
    "cat notes.md memory/travel.md",
    "cat -- notes.md",
    "head -20 notes.md",
    "head -n 20 notes.md",
    "tail -n 50 memory/health.md",
    "nl memory/travel.md",
    "grep -n pattern memory/travel.md",
    'grep -n "Cabot Trail" memory/travel.md',
    "grep -i -l pattern memory/travel.md memory/health.md",
    "grep -A 3 -B 2 pattern notes.md",
    "grep -A3 pattern notes.md",
    # Bundles still clear, and a trailing value flag takes the next token.
    "grep -ni pattern notes.md",
    "grep -ne pattern notes.md",
    "grep -nA 3 pattern notes.md",
    "grep -nf notes.md memory/travel.md",
    # The pattern arrived by option, so the bare argument is a FILE. Reading
    # that backwards left the command with no targets and prompted it.
    "grep -e pattern memory/travel.md",
    'grep -e "Percé\\|^#" memory/travel.md',
    # 13 of the corpus's greps are alternations, and `\` and `|` are not on the
    # character allowlist -- the pattern token is exempt. See
    # _content_verb_targets' docstring for why that one token is safe.
    'grep -n -i "physio\\|9pm\\|cron" memory/health.md',
    'grep -n "Sep 2\\|Sep 3" memory/travel.md',
    # sed, in the ONE shape it is allowed: a bare line-range print. Both real
    # uses in the 191-call corpus are this, and it is the prompt that survived
    # the 2026-09-06 content-verb rule. `,` is already on VETTED_TOKEN_CHARS,
    # so unlike grep's pattern this needed no character exemption.
    "sed -n '1,55p' memory/travel.md",
    "sed -n '210,280p' notes.md",
    # `-n` is conventional, not required: without it sed prints the range twice,
    # which is untidy and not a security property.
    "sed '1,5p' notes.md",
    # 2026-10-02 replay: Marlow searches its notes in the language they are
    # written in, and heading searches carry `#`. Neither means anything to the
    # shell inside a pattern (boundaries.PATTERN_EXTRA_CHARS).
    'grep -n "Percé\\|Gaspé" memory/travel.md',
    'grep -n -i "forillon\\|percé" memory/travel.md',
    'grep -n "^#\\|^##" notes.md',
    'grep -c "#" notes.md',
    # nl's body-style option, attached (boundaries.NL_BODY_STYLE_OPT).
    "nl -ba memory/travel.md",
    "nl -bt notes.md",
])
def test_content_verbs_naming_regular_files_are_vetted(command, content_tree) -> None:
    decision, why = vet_bash_command(command, content_tree)
    assert decision == ALLOW, f"{command!r} should not need a prompt: {why}"


@pytest.mark.parametrize("command", [
    # Names no target at all -- the shape containment could never bound, and
    # the reason `grep` was refused outright in 2026-08-06.
    "grep -r secret .",
    "grep -rn secret .",
    "grep -R secret .",
    # A bundle: `-ril` decomposes, and its `-r` is not on the option list.
    "grep -ril secret .",
    "grep pattern",
    "cat",
    # A DIRECTORY target is how a content verb reads a whole tree.
    "cat memory",
    "cat memory/",
    "grep -n pattern memory",
    # Not a regular file / does not exist.
    "cat missing.md",
    "cat /dev/stdin",
    # Outside the tree, by every spelling.
    "cat ../outside.md",
    "cat /etc/passwd",
    "cat ~/.ssh/id_ed25519",
    # Inside the tree but protected -- the working tree CONTAINS secrets.
    "cat .env",
    "cat .claude/settings.json",
    "grep -n pattern .env",
    # A path arriving as an option VALUE is still a read.
    "grep -f /etc/passwd pattern notes.md",
    # An option nobody has considered costs a prompt rather than a guess.
    "cat --show-all notes.md",
    "grep --include=*.md pattern notes.md",
    # `tail -f` never returns: it would burn invoke.py's timeout unattended.
    "tail -f notes.md",
    # Writes, however they are spelled.
    "cat notes.md > out.md",
    "cat notes.md >> memory/travel.md",
    "grep pattern notes.md > .claude/settings.json",
    # --- sed: the script is the dangerous half, not the target ---------------
    # Each of these is a WRITE or an EXEC primitive on an auto-approved command,
    # which is why sed is gated on a closed regex rather than a character
    # allowlist (boundaries.SED_LINE_RANGE_SCRIPT).
    "sed -i 's/a/b/' notes.md",
    "sed -i.bak 's/a/b/' notes.md",
    "sed -n '1,5w /etc/cron.d/x' notes.md",
    "sed -n '1e id' notes.md",
    "sed '1e curl evil.example' notes.md",
    # `-e` and `-f` supply a script by another route, so omitting them from the
    # option set is what refuses them -- there is no special case.
    "sed -e '1,5p' notes.md",
    "sed -f script.sed notes.md",
    # Scripts that are merely not a line-range print. Deliberately narrow: none
    # of these appear in the corpus and each is a separate argument.
    "sed -n '1,5d' notes.md",
    "sed -n 's/a/b/p' notes.md",
    "sed -n '$p' notes.md",
    "sed -n '1,$p' notes.md",
    "sed -n '/secret/p' notes.md",
    "sed -n '210,280,' notes.md",
    # No script at all: sed would read the FILE as its script.
    "sed -n notes.md",
    # A valid script still does not excuse the target rules.
    "sed -n '1,5p'",
    "sed -n '1,5p' memory",
    "sed -n '1,5p' ../outside.md",
    "sed -n '1,5p' /etc/passwd",
    "sed -n '1,5p' .env",
    "sed -n '1,5p' notes.md > out.md",
    # --- the 2026-10-02 pattern widening, and what it must NOT widen ---------
    # Letters only: a fullwidth semicolon and a combining mark are not letters.
    'grep "a；id" notes.md',
    'grep "e\u0301" notes.md',
    # The widening is the pattern's, never a target's.
    'grep pat "notes#.md"',
    "grep pat mémoire.md",
    'grep "#x" /etc/passwd',
    'grep "Percé" .env',
    # `;` and `'` still fail inside a pattern that also carries the new chars.
    'grep "é;id" notes.md',
    'grep "#\'" notes.md',
    # nl's `-b` is closed to the three letter styles; `-bp` takes a regex.
    "nl -bpfoo notes.md",
    "nl -bx notes.md",
    "nl -ba",
    "nl -ba /etc/passwd",
    # An option VALUE is a shell word: a glob there expands into extra FILE
    # operands. Live until 2026-10-02 (adversarial differential execution).
    "grep -v -e ../outside/* notes.md",
    "grep -v -e .[ec]* notes.md",
    "grep -h -v -e .[ec]* notes.md",
    "grep -A ../outside/* pat notes.md",
    "grep -m {1,2} pat notes.md",
    "head -n ../outside/* notes.md",
    "tail -n .[ec]* notes.md",
    "nl -s ../outside/* notes.md",
    "nl -w .[ec]* notes.md",
    'grep -e "a;id" notes.md',
    # A value-taking flag at the END of a bundle takes the next token: these
    # read the -f file as patterns, never containment-checked (until
    # 2026-10-02, /code-review high).
    "grep -nf /etc/passwd notes.md",
    "grep -vf .env notes.md",
    "grep -of ../outside.md notes.md",
    "grep -nvf .env notes.md",
    # ...and one EARLIER in a bundle takes the rest of it as its value.
    "grep -fn notes.md",
    "grep -en notes.md",
])
def test_content_verbs_that_must_still_prompt(command, content_tree) -> None:
    assert vet_bash_command(command, content_tree)[0] == PROMPT, command


def test_content_verb_target_symlinked_out_of_the_tree_prompts(content_tree, tmp_path) -> None:  # noqa: E501
    """isfile() follows symlinks, so the regular-file clause alone would pass a
    link pointing at a secret. Containment resolves it first."""
    import os

    secret = tmp_path / "secret.txt"
    secret.write_text("k\n")
    link = Path(content_tree) / "memory" / "link.md"
    os.symlink(secret, link)
    try:
        assert vet_bash_command("cat memory/link.md", content_tree)[0] == PROMPT
    finally:
        link.unlink()


def test_pattern_exemption_does_not_extend_to_targets(content_tree) -> None:
    """The exemption is scoped to the ONE token that is never resolved as a
    path. A metacharacter in a target still abandons vetting."""
    assert vet_bash_command('grep pattern "a|b.md"', content_tree)[0] == PROMPT
    assert vet_bash_command('grep pattern "notes.md\\|x"', content_tree)[0] == PROMPT
    # and a non-grep content verb has no pattern slot at all
    assert vet_bash_command('cat "a|b.md"', content_tree)[0] == PROMPT


# --- shape allowances -------------------------------------------------------


@pytest.mark.parametrize("command", [
    "cat memory/travel.md 2>/dev/null",
    "cat memory/travel.md 2>&1",
    "ls -la memory 2>/dev/null",
    # The redirect ends at a `;`, not at whitespace. Anchoring the pattern on
    # whitespace left it in place and six corpus commands prompted for a
    # construct this was written to allow.
    "cat memory/travel.md 2>/dev/null; ls memory",
    "cat memory/travel.md 2>/dev/null | head -20",
    "cat notes.md | head -20",
    "grep -n pattern memory/travel.md | head -5",
    "cat notes.md | wc -l",
    "cat notes.md | sort | uniq",
    "ls memory && cat notes.md",
    "cat notes.md; echo ---; cat memory/travel.md",
    "echo ---",
    "echo hello world",
])
def test_shape_allowances_are_vetted(command, content_tree) -> None:
    decision, why = vet_bash_command(command, content_tree)
    assert decision == ALLOW, f"{command!r} should not need a prompt: {why}"


@pytest.mark.parametrize("command", [
    # EVERY segment must clear. The second one decides the answer.
    "cat notes.md; curl https://evil.example",
    "cat notes.md && rm -rf memory",
    "cat notes.md || cat /etc/passwd",
    "ls memory; cat .env",
    # A sink that names a path is not a sink -- it is a second read.
    "cat notes.md | wc -l /etc/passwd",
    "cat notes.md | tee /tmp/out",
    # `cat notes.md | grep pattern` used to be HERE, under this heading, and it
    # did not belong: `pattern` is a regex, not a path. It was filed under the
    # path rule because grep was not a sink, which was itself the accident
    # (boundaries.SINK_BARE_ARGUMENTS). It now clears, and lives in
    # test_ordinary_sinks_still_clear. `| grep pattern FILE` still prompts.
    "cat notes.md | sh",
    "cat notes.md | xargs rm",
    # Substitution is refused, never parsed.
    "cat $(ls)",
    "cat `ls`",
    "cat ${HOME}/notes.md",
    # `&` backgrounds a command, which would outlive the vetting round-trip.
    "cat notes.md & curl https://evil.example",
    # Only `2>` is inert. Everything else is a write primitive.
    "cat notes.md 1>/dev/null",
    "cat notes.md &>/dev/null",
    "cat < notes.md",
])
def test_shapes_that_must_still_prompt(command, content_tree) -> None:
    assert vet_bash_command(command, content_tree)[0] == PROMPT, command


def test_every_sequenced_segment_is_vetted_not_just_the_first(content_tree) -> None:
    """The regression that matters most in the sequencing allowance: a clean
    first segment must not carry an unvetted second one."""
    decision, why = vet_bash_command("cat notes.md; cat /etc/passwd", content_tree)
    assert decision == PROMPT
    assert "/etc/passwd" in why


# --- differential: what the SHELL does, vs what the vetter thinks -----------
#
# Every previous failure in this module was the same shape -- a static reading
# of a command string disagreeing with what bash would actually run -- and each
# was found by a person noticing, one review pass at a time. This test asks bash
# instead.
#
# The strings below all cleared an earlier draft of the content-verb rule or
# probe its edges. `grep -A \'1;id \' f.md g.md` was ALLOWED before
# 2026-09-06: `\'` is a literal quote to the shell, so the `;` between the two
# of them separates commands and `id` runs, while the vetter's quote scanner
# read one quoted region containing no operator.


ADVERSARIAL_COMMANDS = (
    # the self-review finding, and its neighbours
    r"grep -A \'1;id \' f.md g.md",
    r"grep -e \'x;id \' f.md g.md",
    r"grep -A \'1;id\' f.md",
    r"cat \'f.md;id\' g.md",
    r'grep -A \"1;id \" f.md g.md',
    # ANSI-C quoting, which the scanner also does not implement
    r"grep $'a;id' f.md",
    r"cat $'f.md\x3bid' g.md",
    # escapes that make an operator literal, or a literal an operator
    r"cat f.md \; id",
    r"cat f.md \&\& id",
    r"grep 'a;id' f.md",
    r'grep "a|id" f.md',
    r"cat f.md;id",
    r"cat notes.md |id",
    # a sink's OPTION can be a write primitive -- the `git log --output=`
    # finding one verb later. ALLOWED before 2026-09-06, and it wrote the file.
    r"cat notes.md | sort -o/tmp/marlow-test-pwned",
    r"cat notes.md | sort --output=/tmp/marlow-test-pwned",
    # the constructs the shape allowances accept, with something extra hidden
    r"cat f.md 2>/dev/null; id",
    r"cat notes.md | head -20; id",
    r"cat f.md && id",
)


@pytest.mark.parametrize("command", ADVERSARIAL_COMMANDS)
def test_no_vetted_command_runs_anything_but_its_own_verb(command, tmp_path) -> None:
    """If the vetter ALLOWS it, bash must run only content verbs and sinks.

    Executed for real, against a PATH holding nothing but logging stubs, so the
    assertion is about the shell's parse rather than about anyone's reading of
    it. `id` stands in for "any command the model chose"; if it appears in the
    log for an ALLOWED command, that is unprompted arbitrary execution.
    """
    import shutil
    import subprocess

    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    log = tmp_path / "ran.log"
    for name in ("cat", "grep", "head", "tail", "nl", "wc", "sort", "uniq",
                 "cut", "tr", "ls", "echo", "date", "git", "id", "curl", "rm", "sh"):
        stub = stub_dir / name
        stub.write_text(f'#!/bin/sh\necho {name} >> "{log}"\nexit 0\n')
        stub.chmod(0o755)

    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "f.md").write_text("x\n")
    (tree / "g.md").write_text("y\n")

    decision = vet_bash_command(command, str(tree))[0]
    if decision != ALLOW:
        return  # a prompt is always a safe answer; this test only polices ALLOW

    # An absolute path: PATH below holds only the stubs, so bash could not find
    # itself through it.
    bash = shutil.which("bash") or "/bin/bash"
    subprocess.run(
        [bash, "-c", command],
        cwd=tree,
        env={"PATH": str(stub_dir), "HOME": str(tmp_path)},
        capture_output=True,
        timeout=10,
    )
    ran = set(log.read_text().split()) if log.exists() else set()
    forbidden = ran & {"id", "curl", "rm", "sh"}
    assert not forbidden, (
        f"vet_bash_command ALLOWED {command!r} but bash ran {sorted(forbidden)}"
    )


@pytest.mark.parametrize("command", [
    # A sink is a verb that truncates a stream and opens nothing. An option
    # that names an output file makes it a write, and "anything starting with
    # a dash is an option" is not an option policy -- see
    # boundaries.VETTED_SINK_OPTS.
    "cat notes.md | sort -o/tmp/pwned",
    "cat notes.md | sort --output=/tmp/pwned",
    "cat notes.md | sort -o /tmp/pwned",
    "cat notes.md | sort -o relative-file",
    "cat notes.md | head -c/etc/passwd",
    # A bare argument is a second read, which is what separates a sink from an
    # ordinary command.
    "cat notes.md | wc -l /etc/passwd",
    "cat notes.md | cut -f1 g.md",
    # Not sinks at all.
    "cat notes.md | tee /tmp/out",
    "cat notes.md | xargs cat",
    # --- grep and cat as sinks: the budget is ONE bare token, and only grep's.
    # The second bare token is a FILE, which containment never saw.
    "cat notes.md | grep pat secrets.txt",
    "cat notes.md | grep pat /etc/passwd",
    "cat notes.md | cat secrets.txt",
    "cat notes.md | cat /etc/passwd",
    # `-r` with no file operand does not read stdin -- it recurses the CURRENT
    # DIRECTORY, with no target list for containment to check.
    "cat notes.md | grep -r pat",
    "cat notes.md | grep -R pat",
    "cat notes.md | grep --recursive pat",
    # `-f` reads its patterns from an arbitrary path; `-e` would make the one
    # bare token a file rather than the pattern.
    "cat notes.md | grep -f /etc/passwd",
    "cat notes.md | grep -e pat f.md",
    # The pattern is widened by two characters, not exempted: the quote-desync
    # that allowed command execution must still fail in sink position too.
    "cat notes.md | grep -A \\'1;id \\' f.md",
    'cat notes.md | grep "a;id"',
    # --- the /security-review 2026-09-11 hole, in every shape it was found ---
    # A BOOLEAN option must not absorb the digit that should have spent grep's
    # bare-token budget: if it does, the FILE spends it instead and is read with
    # no prompt. `-v` inverts, so a digit pattern matching nothing dumps the
    # whole file. See boundaries.SINK_VALUE_OPTS.
    "cat notes.md | grep -v 999999 /etc/passwd",
    "cat notes.md | grep -i 9 /etc/passwd",
    "cat notes.md | grep -n 9 ../outside.md",
    "cat notes.md | grep -c 1 ~/.ssh/config",
    "cat notes.md | grep -iv 9 .claude/settings.json",
    "cat notes.md | grep -w 5 .env",
    "cat notes.md | grep -x 9 .git/config",
    # Same fold, other sinks -- these were reachable before grep had a budget
    # and are closed by the same table.
    "cat notes.md | wc -l 5",
    "cat notes.md | cat -n 5",
    # --- `| sed`: only the bare line-range print, nothing riding along -------
    "cat notes.md | sed -n '1,5p' /etc/passwd",
    "cat notes.md | sed -n '1,5p' notes.md",
    "cat notes.md | sed -n '1,5w /tmp/x'",
    "cat notes.md | sed -n '1e id'",
    "cat notes.md | sed -n '31,34p;42p'",
    "cat notes.md | sed -e '1,5p'",
    "cat notes.md | sed -i '1,5p'",
    "cat notes.md | sed '1,5p'",
    "cat notes.md | sed -n",
    "cat notes.md | sed",
    "cat notes.md | sed -n -n '1,5p'",
    # The sink grep's pattern is widened like the content-verb one, no further.
    'cat notes.md | grep "é;id"',
    'cat notes.md | grep "a；id"',
    "cat notes.md | nl -bpfoo",
    # `-ba` has its value attached; the digit after it is a FILE to nl and
    # must not be folded into an option (/security-review 2026-10-02).
    "cat notes.md | nl -ba 5",
    "cat notes.md | nl -bt 12",
])
def test_pipe_sinks_may_not_write_or_read(command, content_tree) -> None:
    """The first stage must be one that CLEARS on its own (`cat notes.md`).
    Until 2026-10-02 every case here opened with `cat f.md`, which is not in
    content_tree, so each prompted on the first stage and the sink rule was
    never exercised -- the 2026-09-11 regressions included. Mutation-checked:
    letting any `sed` stage through now fails 11 of these."""
    assert vet_bash_command(command, content_tree)[0] == PROMPT, command


@pytest.mark.parametrize("command", [
    "cat notes.md | head -20",
    "cat notes.md | head -n 20",
    "cat notes.md | tail -n 5",
    "cat notes.md | wc -l",
    "cat notes.md | sort | uniq -c",
    "cat notes.md | sort -k1",
    # A grep naming NO file reads stdin and opens nothing -- as much a sink as
    # `head`. Its one bare token is the pattern (boundaries.SINK_BARE_ARGUMENTS).
    "cat notes.md | grep pattern",
    "cat notes.md | grep -i pattern",
    'cat notes.md | grep -i "time zone"',
    "cat notes.md | grep -v pattern",
    "cat notes.md | grep -iv node_modules",
    "cat notes.md | grep -A 3 pattern",
    'cat notes.md | grep "a\\|b"',
    "cat notes.md | grep -n pattern | head -20",
    # `| cat` names nothing at all.
    "cat notes.md | cat",
    "cat notes.md | cat -n",
    # The fold still works for options that genuinely TAKE a value, which is
    # the whole point of restricting it rather than deleting it.
    "cat notes.md | grep -m 5 pattern",
    "cat notes.md | grep -A 3 pattern",
    "cat notes.md | grep -B 2 pattern",
    # 2026-10-02 replay.
    "nl -ba memory/travel.md | sed -n '1,80p'",
    "cat notes.md | sed -n '10,20p'",
    'cat notes.md | grep "Percé\\|^#"',
    "cat notes.md | nl -ba",
])
def test_ordinary_sinks_still_clear(command, content_tree) -> None:
    decision, why = vet_bash_command(command, content_tree)
    assert decision == ALLOW, f"{command!r} should not need a prompt: {why}"
