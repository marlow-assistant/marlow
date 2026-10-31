"""`conftest.write_executable`: the suite's fake CLIs and stubs, cached.

macOS runs a Gatekeeper scan (syspolicyd, with a notarization lookup) the first
time each new file is exec'd: ~150ms, one at a time across the whole machine.
The suite wrote a fresh script per test and paid it 232 times a run, ~35s of a
51s parallel run, and the floor more workers could not lower (2026-10-31).
The verdict sticks to the inode, so a hardlink to an already-run script is
free. These pin the parts that keep a shared inode safe to share.
"""
from __future__ import annotations

import errno
import os
import subprocess

import pytest

import conftest
from conftest import write_executable

SCRIPT = "#!/bin/sh\necho hello\n"


@pytest.fixture
def cache(tmp_path, monkeypatch):
    d = tmp_path / "exec-cache"
    monkeypatch.setattr(conftest, "_exec_cache_dir", lambda: d)
    return d


def test_same_text_shares_one_inode(cache, tmp_path):
    a = write_executable(tmp_path / "a" / "tool", SCRIPT)
    b = write_executable(tmp_path / "b" / "tool", SCRIPT)
    assert a.stat().st_ino == b.stat().st_ino
    assert subprocess.run([str(b)], capture_output=True, text=True).stdout == "hello\n"


def test_different_text_gets_its_own_inode(cache, tmp_path):
    a = write_executable(tmp_path / "a", SCRIPT)
    b = write_executable(tmp_path / "b", SCRIPT.replace("hello", "bye"))
    assert a.stat().st_ino != b.stat().st_ino
    assert subprocess.run([str(b)], capture_output=True, text=True).stdout == "bye\n"


def test_replaces_whatever_is_at_the_path(cache, tmp_path):
    p = tmp_path / "tool"
    p.write_text("old")
    write_executable(p, SCRIPT)
    assert p.read_text() == SCRIPT


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through 0o555")
def test_rewriting_a_linked_stub_fails_instead_of_editing_the_cache(cache, tmp_path):
    """Every test with the same script shares the inode. A test that rewrote
    its stub in place would silently change every other test's."""
    a = write_executable(tmp_path / "a", SCRIPT)
    b = write_executable(tmp_path / "b", SCRIPT)
    with pytest.raises(PermissionError):
        a.write_text("#!/bin/sh\necho clobbered\n")
    assert b.read_text() == SCRIPT


def test_a_cache_entry_that_changed_is_rebuilt(cache, tmp_path):
    a = write_executable(tmp_path / "a", SCRIPT)
    a.chmod(0o755)  # through the link: the cache entry itself
    a.write_text("#!/bin/sh\necho tampered\n")
    b = write_executable(tmp_path / "b", SCRIPT)
    assert b.read_text() == SCRIPT
    assert b.stat().st_mode & 0o777 == 0o555


def test_a_cache_dir_others_can_write_is_not_used(cache, tmp_path):
    """On a shared /tmp another user could plant a script under the hash."""
    cache.mkdir()
    cache.chmod(0o777)
    p = write_executable(tmp_path / "tool", SCRIPT)
    assert p.stat().st_nlink == 1
    assert p.read_text() == SCRIPT
    assert os.access(p, os.X_OK)


def test_root_gets_a_plain_file(cache, tmp_path, monkeypatch):
    """Root writes through 0o555, so the shared inode would not be safe."""
    monkeypatch.setattr(conftest.os, "geteuid", lambda: 0)
    p = write_executable(tmp_path / "tool", SCRIPT)
    assert p.stat().st_nlink == 1
    assert p.read_text() == SCRIPT
    assert os.access(p, os.X_OK)


def test_prune_drops_old_entries_nothing_links_to(cache, tmp_path):
    """A script that embeds a per-test path is a new entry every run and never
    reused; without this the cache grows by ~1.5MB a run, forever."""
    import time

    kept_linked = write_executable(tmp_path / "linked", SCRIPT)
    orphan = write_executable(tmp_path / "orphan", SCRIPT + "# orphan\n")
    young = write_executable(tmp_path / "young", SCRIPT + "# young\n")
    entries = {p.read_text(): p for p in cache.iterdir()}
    orphan.unlink()  # pytest rotated its tmp dir away
    young.unlink()
    old = time.time() - 2 * 86400
    for text in (SCRIPT, SCRIPT + "# orphan\n"):
        os.utime(entries[text], (old, old))

    conftest._prune_exec_cache()

    assert entries[SCRIPT].exists()  # old, but a test still links to it
    assert not entries[SCRIPT + "# orphan\n"].exists()
    assert entries[SCRIPT + "# young\n"].exists()  # may be about to be linked
    assert kept_linked.read_text() == SCRIPT


def test_falls_back_to_a_plain_file_when_linking_fails(cache, tmp_path, monkeypatch):
    def no_link(src, dst):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(conftest.os, "link", no_link)
    p = write_executable(tmp_path / "tool", SCRIPT)
    assert p.read_text() == SCRIPT
    assert os.access(p, os.X_OK)
    assert subprocess.run([str(p)], capture_output=True, text=True).stdout == "hello\n"
