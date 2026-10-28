"""The internal and admin APIs on owner-only Unix sockets (config.py,
`internal_socket_path`, `admin_socket_path`): the directory check, the
listeners, and the client."""
from __future__ import annotations

import asyncio
import dataclasses
import os
import stat
import tempfile
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from server import app as app_module
from server.config import Settings
from server.config import settings as base_settings
from server.internal_client import InternalAPIError, post_json


@pytest.fixture
def short_dir():
    # AF_UNIX paths max out at 104 bytes on macOS; pytest's tmp_path can exceed it.
    return Path(tempfile.mkdtemp(prefix="mt-", dir="/tmp"))


def _use_socket(monkeypatch, path: Path, field: str = "internal_socket_path") -> None:
    monkeypatch.setattr(
        app_module, "settings", dataclasses.replace(base_settings, **{field: path})
    )


def test_the_default_is_a_socket_under_the_users_own_marlow_dir(monkeypatch) -> None:
    monkeypatch.delenv("MARLOW_INTERNAL_SOCKET", raising=False)
    assert Settings().internal_socket_path == Path("~/.marlow/run/internal.sock").expanduser()


def test_the_admin_socket_is_its_own_directory(monkeypatch) -> None:
    """A turn can connect to the internal socket; the admin one must live in a
    directory the bwrap namespace can hide without hiding the internal one."""
    monkeypatch.delenv("MARLOW_ADMIN_SOCKET", raising=False)
    fresh = Settings()
    assert fresh.admin_socket_path == Path("~/.marlow/admin/admin.sock").expanduser()
    assert fresh.admin_socket_path.parent != fresh.internal_socket_path.parent


def test_there_is_no_internal_tcp_port_left() -> None:
    """Loopback TCP is what let one instance's turn reach the other's API."""
    assert not hasattr(base_settings, "internal_port")
    config = app_module.build_internal_uvicorn_config()
    assert config.uds == str(base_settings.internal_socket_path)


def test_prepare_creates_the_dir_owner_only(monkeypatch, short_dir) -> None:
    _use_socket(monkeypatch, short_dir / "run" / "internal.sock")
    app_module.prepare_internal_socket_dir()
    st = os.lstat(short_dir / "run")
    assert stat.S_ISDIR(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o700


def test_prepare_tightens_a_loose_existing_dir(monkeypatch, short_dir) -> None:
    run = short_dir / "run"
    run.mkdir()
    run.chmod(0o755)
    _use_socket(monkeypatch, run / "internal.sock")
    app_module.prepare_internal_socket_dir()
    assert stat.S_IMODE(os.lstat(run).st_mode) == 0o700


def test_prepare_refuses_a_symlinked_dir(monkeypatch, short_dir) -> None:
    """A symlink would let whoever chose its target choose who can connect."""
    target = short_dir / "elsewhere"
    target.mkdir(mode=0o700)
    (short_dir / "run").symlink_to(target)
    _use_socket(monkeypatch, short_dir / "run" / "internal.sock")
    with pytest.raises(RuntimeError, match="not a directory"):
        app_module.prepare_internal_socket_dir()


def test_prepare_refuses_a_dir_owned_by_someone_else(monkeypatch, short_dir) -> None:
    (short_dir / "run").mkdir(mode=0o700)
    _use_socket(monkeypatch, short_dir / "run" / "internal.sock")
    me = os.getuid()
    monkeypatch.setattr(app_module.os, "getuid", lambda: me + 1)
    with pytest.raises(RuntimeError, match="not owned"):
        app_module.prepare_internal_socket_dir()


def test_main_prepares_the_dir_before_serving() -> None:
    import inspect

    source = inspect.getsource(app_module.main)
    assert source.index("prepare_internal_socket_dir()") < source.index("build_internal_uvicorn_config()")
    assert source.index("prepare_admin_socket_dir()") < source.index("build_admin_uvicorn_config()")
    assert "uvicorn.Server(admin_config).serve()" in source


def _serve(config: uvicorn.Config):
    server = uvicorn.Server(config)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=lambda: loop.run_until_complete(server.serve()), daemon=True)
    thread.start()
    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.01)
    assert server.started
    return server, loop, thread


@pytest.fixture
def real_internal_server(monkeypatch, short_dir):
    """The production listener config, on a throwaway socket."""
    sock = short_dir / "run" / "internal.sock"
    _use_socket(monkeypatch, sock)
    app_module.prepare_internal_socket_dir()
    server, loop, thread = _serve(app_module.build_internal_uvicorn_config())
    yield sock
    loop.call_soon_threadsafe(setattr, server, "should_exit", True)
    thread.join(timeout=5)


@pytest.fixture
def real_admin_server(monkeypatch, short_dir):
    sock = short_dir / "admin" / "admin.sock"
    _use_socket(monkeypatch, sock, "admin_socket_path")
    app_module.prepare_admin_socket_dir()
    assert stat.S_IMODE(os.stat(sock.parent).st_mode) == 0o700
    server, loop, thread = _serve(app_module.build_admin_uvicorn_config())
    yield sock
    loop.call_soon_threadsafe(setattr, server, "should_exit", True)
    thread.join(timeout=5)


def test_the_admin_listener_issues_invites(real_admin_server) -> None:
    reply = post_json(real_admin_server, "/internal/admin/invite", {"device_name": "t"}, timeout=5)
    assert set(reply) == {"invite"}


def test_the_internal_listener_issues_no_invites(real_internal_server) -> None:
    with pytest.raises(InternalAPIError) as exc_info:
        post_json(real_internal_server, "/internal/admin/invite", {"device_name": "t"}, timeout=5)
    assert exc_info.value.status == 404


def test_a_non_2xx_reply_raises_with_its_status(real_internal_server) -> None:
    with pytest.raises(InternalAPIError) as exc_info:
        post_json(real_internal_server, "/internal/cron/message", {"bad": "body"}, timeout=5)
    assert exc_info.value.status == 422


def test_nothing_listening_is_an_oserror_without_a_status(short_dir) -> None:
    """An OSError, so the hook's existing unreachable-server branch denies."""
    with pytest.raises(OSError) as exc_info:
        post_json(short_dir / "nothing.sock", "/x", {}, timeout=1)
    assert isinstance(exc_info.value, InternalAPIError)
    assert exc_info.value.status is None


@pytest.mark.parametrize("admin_rel", ["run/admin.sock", "run/sub/admin.sock", "admin.sock"])
def test_an_admin_dir_overlapping_the_internal_one_is_refused(monkeypatch, short_dir, admin_rel) -> None:
    monkeypatch.setattr(
        app_module, "settings",
        dataclasses.replace(
            base_settings,
            internal_socket_path=short_dir / "run" / "internal.sock",
            admin_socket_path=short_dir / admin_rel,
        ),
    )
    with pytest.raises(RuntimeError, match="overlaps"):
        app_module.prepare_admin_socket_dir()
