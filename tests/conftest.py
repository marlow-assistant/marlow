"""Sets MARLOW_* env vars to an isolated temp sandbox *before* any `server.*`
module is imported — server/config.py builds its Settings singleton at import
time from os.environ, so this has to happen at conftest module-load time
(pytest imports conftest.py before collecting test files), not inside a
fixture function (which would run too late).
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

# --- fake CLIs and stubs -------------------------------------------------------
#
# macOS runs a Gatekeeper scan (syspolicyd, including a notarization lookup)
# the first time each new file is exec'd: ~150ms, and one at a time across the
# whole machine. The suite wrote a fresh script per test and paid it 232 times
# a run -- ~35s of a 51s parallel run, and the floor that more xdist workers
# could not lower, while a 4-core Linux runner took 30s (2026-10-31). Running
# the file through an interpreter (`sh f`) skips the scan, but the server
# execs claude_bin directly, so the stubs have to be real executables.
#
# The verdict sticks to the inode, so each distinct script is written once,
# into a per-user cache that outlives the run, and hardlinked to where a test
# wants it. A script that embeds a per-test path misses the cache every time:
# pass the path in through the environment instead.


def _exec_cache_dir() -> Path:
    return Path(tempfile.gettempdir()) / f"marlow-test-exec-{os.getuid()}"


def _private_dir(d: Path) -> bool:
    """Ours alone. On a shared /tmp anyone could plant a script under a hash."""
    try:
        d.mkdir(mode=0o700, exist_ok=True)
        st = d.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and st.st_mode & 0o077 == 0


def _cached_executable(text: str) -> Path | None:
    # Root writes straight through the read-only mode below, so a test that
    # rewrote its stub would rewrite every other test's. The scan this exists
    # for is macOS-only, where nobody runs the suite as root.
    if os.geteuid() == 0:
        return None
    d = _exec_cache_dir()
    if not _private_dir(d):
        return None
    data = text.encode()
    entry = d / hashlib.sha256(data).hexdigest()
    try:
        st = entry.lstat()
        # Read-only, so a test rewriting its linked stub fails instead of
        # editing every other test's. A chmod through a link still reaches
        # it, so check rather than trust.
        if stat.S_ISREG(st.st_mode) and st.st_mode & 0o777 == 0o555 and entry.read_bytes() == data:
            return entry
    except FileNotFoundError:
        pass
    except OSError:
        return None
    # Built under a private name and renamed into place, so a parallel worker
    # never sees a half-written entry. Two racing workers each get an inode of
    # their own: one extra scan, once.
    fd, tmp = tempfile.mkstemp(dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o555)
        os.replace(tmp, entry)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        return None
    return entry


def _prune_exec_cache(max_age_seconds: float = 86400) -> None:
    """Drop entries nothing links to that are over a day old. A script with a
    per-test path in it is a new entry every run and never reused, ~1.5MB a
    run. Pruning one still wanted costs a single scan, never a wrong result:
    a worker that loses the race to link it falls back to a plain file."""
    d = _exec_cache_dir()
    if not _private_dir(d):
        return
    cutoff = time.time() - max_age_seconds
    for entry in d.iterdir():
        try:
            st = entry.lstat()
            if st.st_nlink == 1 and st.st_mtime < cutoff:
                entry.unlink()
        except OSError:
            pass


def write_executable(path: Path, text: str) -> Path:
    """`text` at `path`, executable. Hardlinked from the cache when it can be,
    a plain 0o755 file when it can't (no private cache dir, or a cross-device
    link): the test is the same either way, only slower on macOS."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        path.unlink()
    entry = _cached_executable(text)
    if entry is not None:
        try:
            os.link(entry, path)
            return path
        except OSError:
            pass
    path.write_text(text)
    path.chmod(0o755)
    return path


_prune_exec_cache()

_sandbox = Path(tempfile.mkdtemp(prefix="marlow-test-"))

_bin_dir = _sandbox / "bin"
_bin_dir.mkdir()
FAKE_CLAUDE = _bin_dir / "claude"
write_executable(
    FAKE_CLAUDE,
    textwrap.dedent(
        """\
        #!/usr/bin/env python3
        # Fake claude CLI: stands in for the real gate #1-validated contract
        # shape so server/ plumbing can be tested without a live Pro auth
        # session. Does NOT re-verify the CLI contract itself.
        #
        # It honours --output-format because the server now picks between two
        # of them (attended turns stream, everything else does not) -- a fake
        # that ignored the flag would answer plain JSON to a streaming
        # invocation, i.e. it would be testing a CLI that does not exist. The
        # streaming branch is the six-line shape observed on the box, CLI
        # 2.1.235; tests/test_activity_stream.py is where that shape is
        # actually asserted on.
        import json, sys
        envelope = {
            "type": "result",
            "is_error": False,
            "subtype": "success",
            "result": "fake response",
            "session_id": "fake-session-123",
            "permission_denials": [],
        }
        if "stream-json" in sys.argv[1:]:
            for line in [
                {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
                {"type": "system", "subtype": "init"},
                envelope,
            ]:
                print(json.dumps(line), flush=True)
        else:
            print(json.dumps(envelope))
        """
    ),
)

_creds_path = _sandbox / "credentials.json"
_creds_path.write_text(
    json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": "fake",
                "refreshToken": "fake",
                "expiresAt": 9999999999999,
                "refreshTokenExpiresAt": 9999999999999,
                "subscriptionType": "pro",
                "scopes": [],
            }
        }
    )
)

(_sandbox / "SOUL.md").write_text("Test SOUL: a sandbox stand-in identity.\n")
(_sandbox / "USER.md").write_text("Test USER: a sandbox stand-in user profile.\n")
(_sandbox / "memory").mkdir()
(_sandbox / "memory" / "note-1.md").write_text("A sandbox memory entry.\n")

os.environ["MARLOW_CLAUDE_BIN"] = str(FAKE_CLAUDE)
os.environ["MARLOW_CREDENTIALS_PATH"] = str(_creds_path)
os.environ["MARLOW_DATA_DIR"] = str(_sandbox / "data")
os.environ["MARLOW_LOG_DIR"] = str(_sandbox / "logs")
os.environ["MARLOW_ATTACHMENTS_DIR"] = str(_sandbox / "attachments")
os.environ["MARLOW_WORKING_DIR"] = str(_sandbox)
os.environ["MARLOW_PUBLIC_BIND_HOST"] = "127.0.0.1"
os.environ["MARLOW_PUBLIC_PORT"] = "0"
# Under /tmp, not the sandbox: macOS's TMPDIR is long enough to push a socket
# path past AF_UNIX's 104-byte limit.
os.environ["MARLOW_INTERNAL_SOCKET"] = str(
    Path(tempfile.mkdtemp(prefix="mt-", dir="/tmp")) / "run" / "internal.sock"
)
# Confinement is ON by default in production (server/config.py). Off here for
# the same reason it is off on a dev Mac: bwrap is Linux-only and absent on CI
# runners, and with it on, argv[0] is bwrap, so every invocation test dies with
# "executable not found" before reaching the fake claude above. Nothing under
# test is a real secret. tests/test_confinement.py builds the argv directly and
# asserts the production default separately, so this line cannot hide a
# regression in the flag itself.
os.environ["MARLOW_BWRAP_ENABLED"] = "0"


# --- the stand-in box paths ---------------------------------------------------
#
# The policy suites need a realistic absolute path to reason about. The obvious
# choice is the box's own `/home/marlow`. **Do not use it, and do not "fix"
# these back to it.**
#
# Speed: policy decisions call Path.resolve(), which walks every component with
# lstat(). On macOS `/home` is an autofs automount point (`map auto_home on
# /System/Volumes/Data/home`). `/home` itself stats in microseconds, but
# anything *under* it costs ~7ms while the automounter tries and fails to mount
# a home directory that is not there. One vet_bash_command() call makes ~570
# such lookups, so test_bash_policy.py + test_file_policy.py alone spent 53s of
# a 75s suite waiting on the automounter (measured 2026-08-22, macOS only —
# on the Linux box the same lstat is a fast ENOENT).
#
# Correctness: on the box `/home/marlow/marlow-identity` is a REAL directory,
# so a suite run there resolves these against live paths instead of the
# nowhere-in-particular they are meant to be. Same trap the `_secret` helper in
# test_bash_policy.py and test_agent_config_inside_the_tree_is_not_vetted call
# out — a hardcoded box path behaves differently on the box.
#
# Any prefix outside `/home` works. `/opt` is FHS-standard and never automounted.
FAKE_HOME = "/opt/marlow"
FAKE_TREE = f"{FAKE_HOME}/marlow-identity"


@pytest.fixture(autouse=True)
def _isolated_session_state():
    """`session_state` is a process-wide singleton backed by a file, so without
    this a session retired in one test stays retired for every test after it,
    and an attended session id leaks into cases that assert on its absence.

    Autouse rather than opt-in: the leak is invisible in the failing test (it
    fails on state some *earlier* file created) and would be re-diagnosed from
    scratch each time. Cheap — one unlink and one empty read per test.
    """
    from server.config import settings
    from server.session_state import session_state

    settings.session_state_path.unlink(missing_ok=True)
    session_state.reload()
    yield
    settings.session_state_path.unlink(missing_ok=True)
    session_state.reload()


@pytest.fixture(autouse=True)
def _isolated_usage_state():
    """`usage_state` is a process-wide singleton and every queue-driven turn in
    the suite writes to it, so without this a reading from one test is visible
    to the next test's /v1/usage. Same autouse-not-opt-in reasoning as above."""
    from server.usage_state import usage_state

    usage_state.reset_for_tests()
    yield
    usage_state.reset_for_tests()


@pytest.fixture(autouse=True)
def _isolated_location():
    """`location_store` is process-wide and every /v1/message writes to it, so
    without this one test's location turns up in a later test's context block.
    Same autouse-not-opt-in reasoning as above."""
    from server.location import location_store

    location_store._reset_for_tests()
    yield
    location_store._reset_for_tests()


@pytest.fixture(autouse=True)
def _isolated_preferences():
    """`preferences_store` is one file under the session-wide data_dir, so one
    test's face would otherwise be the next test's starting state."""
    from server.preferences import preferences_store

    preferences_store._reset_for_tests()
    yield
    preferences_store._reset_for_tests()


@pytest.fixture(autouse=True)
def _isolated_ideas():
    """The ideas store and its responses live under the session-wide sandbox
    working dir, so one test's ideas would otherwise be open in the next."""
    import shutil

    from server.config import settings

    shutil.rmtree(settings.ideas_path.parent, ignore_errors=True)
    yield
    shutil.rmtree(settings.ideas_path.parent, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolated_transcript():
    """The transcript store is a file under the session-wide sandbox data_dir
    and `_drain` now writes to it on every turn, so without this a row (and its
    UNIQUE client_msg_id) from one test is visible to, and collides with, every
    test after it. Same reasoning as `_isolated_session_state` above, same
    autouse-not-opt-in: the leak shows up in a different test than the one that
    caused it."""
    import shutil

    from server import transcript

    def _wipe() -> None:
        # The module remembers which db paths it has set up; deleting the file
        # without forgetting that would leave the next test a schemaless db.
        transcript._initialised.clear()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{transcript.db_path()}{suffix}").unlink(missing_ok=True)
        shutil.rmtree(transcript.attachments_dir(), ignore_errors=True)

    _wipe()
    yield
    _wipe()


@pytest.fixture
def sandbox() -> Path:
    return _sandbox


@pytest.fixture
def pair_device():
    """Factory: runs the real pairing flow (issue_invite -> redeem)
    against the live pairing_store singleton and returns a valid
    token. Call with a device_name to pair more than one device in a single
    test (e.g. testing fan-out to multiple paired devices)."""
    from server.pairing import pairing_store

    def _pair(device_name: str = "test-device") -> str:
        return pairing_store.redeem(pairing_store.issue_invite(device_name), device_name)

    return _pair


@pytest.fixture
def paired_device_token(pair_device):
    """Single-token convenience wrapper around pair_device for tests that
    only need one paired device."""
    return pair_device()


# --- throwaway git repos -------------------------------------------------------
#
# Several test modules build a scratch git repo and commit into it
# (test_identity_git, test_identity_push, test_deploy, test_pre_push_hook).
# Those sequences are `git init` + `git add -A` + `git commit` — which, pointed
# at the wrong directory, rewrite whatever repo they land in. That is not
# hypothetical: on 2026-08-20 the feature/auto-compaction branch was flattened to
# an empty tree by exactly these fixtures' commands being run against the real
# working tree, and the real work survived only because it was already
# committed.
#
# `git init` inside an existing work tree is silent and successful, so nothing
# in the sequence itself objects. This helper is where the objection lives.


class UnsafeRepoTarget(AssertionError):
    """Refused to build a scratch repo somewhere that isn't disposable."""


def safe_git_init(path: Path, *args: str) -> Path:
    """`git init` at `path`, refusing any target that isn't a throwaway dir.

    Two ways to be unsafe, and the second is the one that actually bit us:
      1. the path is inside this source repo, or otherwise not under a temp root
      2. the path is already inside *some* git work tree — the case where
         `git init` quietly succeeds and later commits eat that repo instead
    """
    path = Path(path).resolve()
    repo_root = Path(__file__).resolve().parent.parent
    tmp_root = Path(tempfile.gettempdir()).resolve()

    if repo_root == path or repo_root in path.parents:
        raise UnsafeRepoTarget(
            f"refusing to git-init inside the source repo: {path}"
        )
    if tmp_root != path and tmp_root not in path.parents:
        raise UnsafeRepoTarget(
            f"refusing to git-init outside {tmp_root} (a temp root): {path}"
        )

    # Nearest existing ancestor: `path` itself usually doesn't exist yet, and
    # git resolves upward regardless, so ask from somewhere that does exist.
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    enclosing = subprocess.run(
        ["git", "-C", str(probe), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if enclosing.returncode == 0:
        raise UnsafeRepoTarget(
            f"refusing to git-init inside an existing work tree "
            f"({enclosing.stdout.strip()}): {path}"
        )

    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(path), "init", "-q", *args], check=True
    )
    return path


@pytest.fixture(autouse=True)
def never_touch_the_developers_crontab(monkeypatch):
    """The suite must never run the real `crontab`.

    Not hypothetical: once `entrypoint.py` re-rendered after a prune
    (2026-08-31, defect 3), the one-off tests reached `install()` for real and
    shelled out on the dev machine — which, on a box with no crontab of its
    own, is one `crontab -` away from installing a table nobody asked for.

    A test that genuinely exercises install() replaces `subprocess.run` itself;
    monkeypatch inside the test overrides this default.
    """
    import server.schedules

    def refuse(argv, **kwargs):
        raise AssertionError(f"the test suite must not run the real crontab: {argv!r}")

    # Swap the module's `subprocess` REFERENCE, not `subprocess.run` itself --
    # the latter is the one shared module object every other test's real
    # subprocess call goes through.
    monkeypatch.setattr(
        server.schedules, "subprocess", SimpleNamespace(run=refuse, TimeoutExpired=subprocess.TimeoutExpired)
    )


@pytest.fixture
def system_zone(monkeypatch):
    """Pin the SYSTEM-local zone, which is the only zone cron understands.

    Debian's cron ignores `CRON_TZ` entirely (defect 1, 2026-08-30), so the
    renderer converts the store's zone into this one. Every render assertion
    below is therefore a two-zone claim and has to say what local is.
    """

    def _use(name: str) -> None:
        monkeypatch.setenv("TZ", name)
        time.tzset()

    yield _use
    monkeypatch.undo()
    time.tzset()


class InternalStub:
    """A stand-in internal API on a real Unix socket.

    Set `reply` to (status, json body); `requests` collects (path, content
    type, decoded body) for each POST. Serves `settings.internal_socket_path`
    unless given another path.
    """

    def __init__(self, path: Path) -> None:
        import http.server
        import socketserver
        import threading

        self.path = path
        self.reply: tuple[int, object] = (202, {"turn_id": "stub-turn"})
        self.requests: list[tuple[str, str, object]] = []
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["content-length"]))
                stub.requests.append((self.path, self.headers["content-type"], json.loads(body)))
                self._send(*stub.reply)

            def do_GET(self):
                stub.requests.append((self.path, None, None))
                self._send(*stub.reply)

            def _send(self, status, payload):
                out = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def address_string(self):  # AF_UNIX peers have no address
                return "unix"

            def log_message(self, *a):
                pass

        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists() or path.is_symlink():
            path.unlink()
        self._server = socketserver.ThreadingUnixStreamServer(str(path), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self.path.unlink(missing_ok=True)


@pytest.fixture
def internal_stub():
    from server.config import settings

    stub = InternalStub(settings.internal_socket_path)
    yield stub
    stub.close()
