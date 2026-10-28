"""`safe_git_init`'s refusal to build a scratch repo in a real one.

The fixtures in test_identity_git / test_identity_push / test_pre_push_hook run
`git init` + `git add -A` + `git commit`. Aimed at a live working tree that
sequence rewrites it, and `git init` inside an existing work tree succeeds
silently, so the destructive path is also the quiet one. These pin the refusals.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import UnsafeRepoTarget, safe_git_init

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git required")

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_builds_a_usable_repo_in_a_temp_dir(tmp_path):
    d = safe_git_init(tmp_path / "scratch", "-b", "main")
    assert (d / ".git").is_dir()
    head = subprocess.run(
        ["git", "-C", str(d), "symbolic-ref", "--short", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    assert head.stdout.strip() == "main"


def test_bare_repos_still_work(tmp_path):
    d = safe_git_init(tmp_path / "origin.git", "--bare", "-b", "main")
    assert (d / "HEAD").is_file()


def test_refuses_a_target_inside_an_existing_work_tree(tmp_path):
    """The case that actually flattened feature/auto-compaction."""
    outer = safe_git_init(tmp_path / "outer", "-b", "main")
    with pytest.raises(UnsafeRepoTarget, match="existing work tree"):
        safe_git_init(outer / "nested", "-b", "main")


def test_refuses_the_source_repo_itself(tmp_path):
    with pytest.raises(UnsafeRepoTarget):
        safe_git_init(REPO_ROOT, "-b", "main")


def test_refuses_a_path_inside_the_source_repo(tmp_path):
    with pytest.raises(UnsafeRepoTarget):
        safe_git_init(REPO_ROOT / "server" / "scratch", "-b", "main")


def test_refuses_a_target_outside_any_temp_root(tmp_path):
    with pytest.raises(UnsafeRepoTarget, match="temp root"):
        safe_git_init(Path.home() / "definitely-not-a-test-repo", "-b", "main")


def test_refusal_creates_nothing(tmp_path):
    """A guard that half-runs before objecting would be worse than none."""
    outer = safe_git_init(tmp_path / "outer", "-b", "main")
    target = outer / "nested"
    with pytest.raises(UnsafeRepoTarget):
        safe_git_init(target, "-b", "main")
    assert not target.exists()
