"""`.githooks/pre-push`'s skip logic: docs-only, deletions, new branches.

Worth testing at all — a hook is not usually — because of what it gates. CI is
OFF (docs/marlow-v2-workflow.md), so this hook is the *only* thing standing
between a red suite and `main`. A bug in the skip logic doesn't announce
itself: it silently stops running the tests, every push goes green, and the
first symptom is a broken box. So the asymmetry is pinned here — over-running
the suite wastes ~20s, over-skipping it removes the gate.

The hook's decision function is extracted and exercised directly rather than
running the hook end to end, which would invoke the real pytest.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import UnsafeRepoTarget, safe_git_init

HOOK = Path(__file__).resolve().parent.parent / ".githooks" / "pre-push"
ZERO = "0" * 40

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")


def _extract(name: str) -> str:
    m = re.search(rf"^{name}\(\) \{{.*?^\}}", HOOK.read_text(), re.S | re.M)
    assert m, f"could not find {name}() in the hook — did it get renamed?"
    return m.group(0)


def _fn_file(tmp_path_factory, name: str) -> Path:
    """`name` plus the push_base helper it calls. PUSH_REMOTE defaults to
    origin as in the hook; a test overrides it to simulate no remote main."""
    out = tmp_path_factory.mktemp("hookfn") / f"{name}.sh"
    out.write_text(
        'PUSH_REMOTE="${PUSH_REMOTE:-origin}"\n'
        + _extract("push_base") + "\n" + _extract(name) + "\n"
    )
    return out


@pytest.fixture(scope="module")
def decide_fn(tmp_path_factory) -> Path:
    """The `docs_only_push` function on its own, without the pytest half."""
    return _fn_file(tmp_path_factory, "docs_only_push")


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> dict:
    """A throwaway repo with one commit per shape we care about."""
    d = tmp_path_factory.mktemp("repo")

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=d, capture_output=True, text=True, check=True
        ).stdout.strip()

    safe_git_init(d, "-b", "main")

    def commit(path: str, msg: str) -> str:
        f = d / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x\n")
        git("add", "-A")
        # -c, not `git config`: see the note in tests/conftest.py.
        git("-c", "user.name=T", "-c", "user.email=t@example.com",
            "commit", "-q", "-m", msg)
        return git("rev-parse", "HEAD")

    shas = {"base": commit("server/thing.py", "base")}
    shas["docs"] = commit("docs/tasks/a.md", "docs only")
    shas["readme"] = commit("README.md", "top-level md")
    shas["identity"] = commit("identity/SOUL.md", "identity template")
    shas["code"] = commit("server/other.py", "code")
    shas["web"] = commit("web/src/thing.ts", "web client")
    # What a new branch is diffed against: the remote's main, as last fetched.
    git("update-ref", "refs/remotes/origin/main", shas["docs"])
    return {"dir": d, **shas}


def _decides(decide_fn, repo, local_sha: str, remote_sha: str, remote="origin") -> str:
    """'skip' or 'run'."""
    r = subprocess.run(
        ["bash", "-c", f"PUSH_REMOTE={remote}; source {decide_fn}; "
                       "docs_only_push && echo skip || echo run"],
        cwd=repo["dir"],
        input=f"refs/heads/topic {local_sha} refs/heads/topic {remote_sha}\n",
        capture_output=True,
        text=True,
    )
    return r.stdout.strip()


def test_docs_only_push_skips_the_suite(decide_fn, repo):
    assert _decides(decide_fn, repo, repo["docs"], repo["base"]) == "skip"


def test_top_level_markdown_counts_as_docs(decide_fn, repo):
    assert _decides(decide_fn, repo, repo["readme"], repo["docs"]) == "skip"


def test_identity_templates_are_NOT_docs(decide_fn, repo):
    """identity/SOUL.md, CLAUDE.md and USER.md are seeded by
    deploy/seed-identity.sh and asserted on by tests/test_deploy.py, so they
    are under test despite being markdown. This is the case that makes a
    blanket '*.md' rule wrong."""
    assert _decides(decide_fn, repo, repo["identity"], repo["readme"]) == "run"


def test_code_push_runs_the_suite(decide_fn, repo):
    assert _decides(decide_fn, repo, repo["code"], repo["identity"]) == "run"


def test_mixed_docs_and_code_runs_the_suite(decide_fn, repo):
    assert _decides(decide_fn, repo, repo["code"], repo["base"]) == "run"


# A new branch (remote sha all zeros) diffs against its merge-base with
# origin/main, which the fixture points at the "docs" commit.

def test_new_docs_only_branch_skips_the_suite(decide_fn, repo):
    """origin/main..readme is just README.md."""
    assert _decides(decide_fn, repo, repo["readme"], ZERO) == "skip"


def test_new_branch_with_code_runs_the_suite(decide_fn, repo):
    assert _decides(decide_fn, repo, repo["code"], ZERO) == "run"


def test_new_branch_sees_unpushed_main_commits(decide_fn, repo):
    """Branched from a local main that is ahead of origin/main: those commits
    have never been gated, and the merge-base with origin/main includes them.
    identity's own commit is the only non-docs change in docs..identity."""
    assert _decides(decide_fn, repo, repo["identity"], ZERO) == "run"


@pytest.mark.parametrize(
    "local_key,remote_sha,remote,why",
    [
        ("readme", ZERO, "nosuch", "new branch, and no remote main to find a merge-base with"),
        ("docs", "dead" * 10, "origin", "remote sha this clone has never seen"),
    ],
)
def test_ambiguity_resolves_toward_running(decide_fn, repo, local_key, remote_sha, remote, why):
    assert _decides(decide_fn, repo, repo[local_key], remote_sha, remote) == "run", why


def test_empty_stdin_runs_the_suite(decide_fn, repo):
    r = subprocess.run(
        ["bash", "-c", f"source {decide_fn}; docs_only_push && echo skip || echo run"],
        cwd=repo["dir"],
        input="",
        capture_output=True,
        text=True,
    )
    assert r.stdout.strip() == "run"


# --- the runner-resolution half ------------------------------------------------
#
# Same asymmetry as the skip logic above, and the reason this half is now
# pinned: it used to `exit 0` when no pytest was found, so a clone or worktree
# without a .venv pushed green without running a thing. A missing runner is the
# gate being OFF, not the gate passing.


@pytest.fixture(scope="module")
def resolve_fn(tmp_path_factory) -> Path:
    """The pytest-resolution block, without actually running the suite."""
    src = HOOK.read_text()
    m = re.search(r'^if \[ -x "\.venv/bin/pytest" \]; then.*?^fi', src, re.S | re.M)
    assert m, "could not find the pytest-resolution block — did the hook change?"
    out = tmp_path_factory.mktemp("resolve") / "resolve.sh"
    out.write_text(m.group(0) + '\necho "resolved:$PYTEST"\n')
    return out


def _resolve(resolve_fn, cwd, path_env: str):
    # bash by absolute path: PATH here is the thing under test, and pointing it
    # at an empty dir would otherwise hide the interpreter too.
    return subprocess.run(
        [shutil.which("bash"), str(resolve_fn)],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={"PATH": path_env},
    )


def test_missing_pytest_blocks_the_push(resolve_fn, tmp_path):
    """No runner anywhere: the push must be refused, not waved through."""
    empty = tmp_path / "emptybin"
    empty.mkdir()
    r = _resolve(resolve_fn, tmp_path, str(empty))
    assert r.returncode == 1, f"missing pytest must block, got {r.returncode}"
    assert "push blocked" in r.stderr
    assert "resolved:" not in r.stdout


def test_missing_pytest_says_how_to_arm_it(resolve_fn, tmp_path):
    """A block that doesn't say how to fix it just invites --no-verify."""
    empty = tmp_path / "emptybin"
    empty.mkdir()
    r = _resolve(resolve_fn, tmp_path, str(empty))
    assert "venv" in r.stderr
    assert "--no-verify" in r.stderr


def test_venv_pytest_is_preferred(resolve_fn, tmp_path):
    """A local .venv wins over whatever happens to be on PATH."""
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "pytest").write_text("#!/bin/sh\n")
    (venv_bin / "pytest").chmod(0o755)
    r = _resolve(resolve_fn, tmp_path, "/usr/bin:/bin")
    assert r.returncode == 0
    assert "resolved:.venv/bin/pytest" in r.stdout


def test_path_pytest_is_the_fallback(resolve_fn, tmp_path):
    """No .venv but pytest on PATH is still a real runner — allow it."""
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    (fake_bin / "pytest").write_text("#!/bin/sh\n")
    (fake_bin / "pytest").chmod(0o755)
    r = _resolve(resolve_fn, tmp_path, f"{fake_bin}:/usr/bin:/bin")
    assert r.returncode == 0
    assert "resolved:pytest" in r.stdout


# --- the hook's own environment ------------------------------------------------
#
# Git exports GIT_DIR (and friends) into hook processes. Left in place they make
# `conftest.safe_git_init` resolve every temp directory to this repo and refuse
# it, so the suite is green from a shell and red from inside the hook — which
# reads as a broken gate and invites `--no-verify`. It did on 2026-08-23. Both
# halves are pinned: that the leak really does break the fixtures, and that the
# hook scrubs it.


def test_an_inherited_git_dir_really_does_break_scratch_repos(monkeypatch, tmp_path):
    """Why the scrub exists. Delete the unset and this is what comes back."""
    monkeypatch.setenv("GIT_DIR", str(Path(__file__).resolve().parent.parent / ".git"))
    with pytest.raises(UnsafeRepoTarget):
        safe_git_init(tmp_path / "scratch")


def test_the_hook_scrubs_gits_exported_environment(tmp_path_factory, tmp_path):
    """The unset has to run before the suite does, not merely exist."""
    src = HOOK.read_text()
    m = re.search(
        r"^unset GIT_DIR .*?^if \[ -x \"\.venv/bin/pytest\" \]; then.*?^fi",
        src, re.S | re.M,
    )
    assert m, "could not find the env scrub ahead of the pytest block — did the hook change?"
    script = tmp_path_factory.mktemp("scrub") / "scrub.sh"
    script.write_text("set -euo pipefail\n" + m.group(0) + '\necho "gitdir:${GIT_DIR-unset}"\n')

    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    (fake_bin / "pytest").write_text("#!/bin/sh\n")
    (fake_bin / "pytest").chmod(0o755)

    r = subprocess.run(
        [shutil.which("bash"), str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={"PATH": f"{fake_bin}:/usr/bin:/bin", "GIT_DIR": "/somewhere/.git",
             "GIT_INDEX_FILE": "/somewhere/.git/index"},
    )
    assert r.returncode == 0, r.stderr
    assert "gitdir:unset" in r.stdout


# --- the web client's tests (push_touches_web) ----------------------------------

@pytest.fixture(scope="module")
def web_fn(tmp_path_factory) -> Path:
    return _fn_file(tmp_path_factory, "push_touches_web")


def _web(web_fn, repo, stdin: str, remote="origin") -> str:
    r = subprocess.run(
        ["bash", "-c", f"PUSH_REMOTE={remote}; source {web_fn}; "
                       "push_touches_web && echo run || echo skip"],
        cwd=repo["dir"],
        input=stdin,
        capture_output=True,
        text=True,
    )
    return r.stdout.strip()


def test_a_web_change_runs_the_web_tests(web_fn, repo):
    assert _web(web_fn, repo, f"refs/heads/main {repo['web']} refs/heads/main {repo['code']}\n") == "run"


def test_a_server_only_change_skips_the_web_tests(web_fn, repo):
    """The point of the check: a server change keeps the suite at ~20s."""
    assert _web(web_fn, repo, f"refs/heads/main {repo['code']} refs/heads/main {repo['base']}\n") == "skip"


def test_a_new_server_only_branch_skips_the_web_tests(web_fn, repo):
    """The other half of the 2026-10-05 fix: a first push used to demand
    web/node_modules, which fresh worktrees lack, for any change at all."""
    assert _web(web_fn, repo, f"refs/heads/topic {repo['code']} refs/heads/topic {ZERO}\n") == "skip"


def test_a_new_web_branch_runs_the_web_tests(web_fn, repo):
    assert _web(web_fn, repo, f"refs/heads/topic {repo['web']} refs/heads/topic {ZERO}\n") == "run"


@pytest.mark.parametrize(
    "remote_sha,remote,why",
    [
        (ZERO, "nosuch", "new branch, and no remote main to find a merge-base with"),
        ("dead" * 10, "origin", "remote sha this clone has never seen"),
    ],
)
def test_web_ambiguity_resolves_toward_running(web_fn, repo, remote_sha, remote, why):
    stdin = f"refs/heads/main {repo['code']} refs/heads/main {remote_sha}\n"
    assert _web(web_fn, repo, stdin, remote) == "run", why


def test_web_empty_stdin_runs_the_web_tests(web_fn, repo):
    assert _web(web_fn, repo, "") == "run"


def test_both_checks_read_the_same_refs():
    """Git writes the refs to stdin once. If one check consumed it, the other
    would see empty stdin and fall back to its ambiguity answer -- for the web
    check that is "run", which hides the bug as a slow push rather than a
    skipped gate, but for docs_only_push reading second it would be the
    opposite. Both must be fed from the one captured copy."""
    src = HOOK.read_text()
    assert 'PUSH_REFS="$(cat)"' in src
    assert 'docs_only_push <<< "$PUSH_REFS"' in src
    assert 'push_touches_web <<< "$PUSH_REFS"' in src


def test_the_web_gate_typechecks_as_well_as_testing():
    """The box builds with `tsc -b`; Vitest does not check types. Without the
    typecheck here a type error is green locally and red on the box."""
    src = HOOK.read_text()
    assert "npm run typecheck --silent && npm test --silent" in src


# --- branch deletions ----------------------------------------------------------
#
# A deletion pushes no commits. It used to fall through to "no range, run
# everything" -- ~80s per deleted branch.

@pytest.fixture(scope="module")
def deletions_fn(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("hookfn") / "deletions.sh"
    out.write_text(_extract("deletions_only_push"))
    return out


def _deletions(deletions_fn, repo, stdin: str) -> str:
    r = subprocess.run(
        ["bash", "-c", f"source {deletions_fn}; deletions_only_push && echo skip || echo run"],
        cwd=repo["dir"], input=stdin, capture_output=True, text=True,
    )
    return r.stdout.strip()


def test_deleting_branches_skips_everything(deletions_fn, repo):
    stdin = "".join(
        f"(delete) {ZERO} refs/heads/{b} {repo['code']}\n" for b in ("a", "b", "c")
    )
    assert _deletions(deletions_fn, repo, stdin) == "skip"


def test_a_deletion_alongside_a_real_push_is_not_skipped(deletions_fn, repo):
    stdin = (f"(delete) {ZERO} refs/heads/old {repo['code']}\n"
             f"refs/heads/topic {repo['code']} refs/heads/topic {repo['base']}\n")
    assert _deletions(deletions_fn, repo, stdin) == "run"


def test_empty_stdin_is_not_a_deletion_push(deletions_fn, repo):
    assert _deletions(deletions_fn, repo, "") == "run"


# --- end to end ------------------------------------------------------------------
#
# The whole hook, in the fixture repo, with a fake pytest that leaves a marker
# and no npm at all. This is the check that the early exits sit where they
# should: before the suite, and before the web half's runner checks.

def _run_hook(repo, tmp_path, stdin: str, fake_pytest: str | None = None):
    """`fake_pytest` is the fake runner's shell body; by default it only
    leaves the marker."""
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    marker = tmp_path / "pytest-ran"
    (fake_bin / "pytest").write_text("#!/bin/sh\n" + (fake_pytest or f"touch {marker}\n"))
    (fake_bin / "pytest").chmod(0o755)
    git_dir = str(Path(shutil.which("git")).parent)
    r = subprocess.run(
        [shutil.which("bash"), str(HOOK), "origin", "unused-url"],
        cwd=repo["dir"], input=stdin, capture_output=True, text=True,
        env={"PATH": f"{fake_bin}:{git_dir}:/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    return r, marker.exists()


def test_hook_deletion_push_exits_before_the_suite(repo, tmp_path):
    r, ran = _run_hook(repo, tmp_path, f"(delete) {ZERO} refs/heads/old {repo['code']}\n")
    assert r.returncode == 0, r.stderr
    assert not ran


def test_hook_new_server_branch_needs_no_web_runner(repo, tmp_path):
    """No npm on PATH, no web/node_modules: a new branch that leaves web/
    alone must still push, after running pytest."""
    if (repo["dir"] / "web" / "node_modules").exists():
        pytest.skip("fixture unexpectedly has node_modules")
    r, ran = _run_hook(repo, tmp_path, f"refs/heads/topic {repo['code']} refs/heads/topic {ZERO}\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert ran


# --- parallel when it can be ---------------------------------------------------
#
# The suite takes ~2 minutes serially on a dev Mac and the hook runs it on every
# code push, so the hook hands it to pytest-xdist. A venv made before xdist was
# a dev dependency doesn't have it, and `-n` is then a usage error -- which
# would block every push from that clone and point straight at --no-verify. So
# the hook asks the runner first, and without xdist runs the suite serially:
# slower, but still the whole gate.

def _run_hook_with_runner(repo, tmp_path, *, has_xdist: bool):
    calls = tmp_path / "pytest-calls"
    help_text = "  -n numprocesses, --numprocesses=numprocesses" if has_xdist else "  -q, --quiet"
    r, _ = _run_hook(
        repo, tmp_path, f"refs/heads/topic {repo['code']} refs/heads/topic {ZERO}\n",
        fake_pytest=(
            f'echo "$*" >> {calls}\n'
            'case " $* " in *" --help "*) echo "' + help_text + '" ;; esac\n'
        ),
    )
    suite_runs = [c for c in calls.read_text().splitlines() if "--help" not in c.split()]
    return r, suite_runs


def test_hook_runs_the_suite_in_parallel_when_xdist_is_there(repo, tmp_path):
    r, runs = _run_hook_with_runner(repo, tmp_path, has_xdist=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(runs) == 1, runs
    assert "-n" in runs[0].split(), runs


def test_hook_falls_back_to_serial_without_xdist(repo, tmp_path):
    """No xdist: the suite still runs, serially, and the hook says how to fix it."""
    r, runs = _run_hook_with_runner(repo, tmp_path, has_xdist=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(runs) == 1, runs
    assert "-n" not in runs[0].split(), runs
    assert "pytest-xdist" in r.stdout + r.stderr
