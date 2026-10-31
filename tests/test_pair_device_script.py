"""deploy/pair-device.sh against a local stub of the admin endpoint."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import write_executable

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "pair-device.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("curl") is None, reason="bash and curl required"
)


@pytest.fixture
def stub(internal_stub):
    internal_stub.reply = (200, {"invite": "SECRET-INVITE"})
    return str(internal_stub.path), internal_stub.requests


def _run(sock: str, *args: str, tailnet: str | None = "box.example.ts.net", path: str | None = None):
    env = {"PATH": path or os.environ["PATH"], "MARLOW_ADMIN_SOCKET": sock}
    if tailnet is not None:
        env["MARLOW_TAILNET_NAME"] = tailnet
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env)


def test_prints_the_link_with_the_invite_in_the_fragment(stub) -> None:
    sock, seen = stub
    r = _run(sock)
    assert r.returncode == 0, r.stderr
    assert "https://box.example.ts.net:8443/#pair=SECRET-INVITE" in r.stdout
    assert "10 minutes" in r.stdout
    path, ctype, body = seen[0]
    assert path == "/internal/admin/invite"
    assert ctype == "application/json"
    assert body == {"device_name": "web browser"}


def test_the_invite_is_never_in_a_query_string(stub) -> None:
    """A query string would reach the access logs and the Funnel path."""
    sock, _ = stub
    r = _run(sock)
    assert "?pair=" not in r.stdout and "?invite=" not in r.stdout


def test_hostile_device_name_is_json_escaped(stub) -> None:
    sock, seen = stub
    name = 'Mike\'s "phone" \\ back\\slash\n$(rm -rf /) `x`'
    r = _run(sock, name)
    assert r.returncode == 0, r.stderr
    assert seen[0][2] == {"device_name": name}


def test_fails_clearly_when_nothing_is_listening(tmp_path) -> None:
    r = _run(str(tmp_path / "nothing.sock"))
    assert r.returncode != 0
    assert "marlow-server running" in r.stderr
    assert r.stdout.strip() == ""


def test_no_tailnet_name_prints_a_placeholder_not_another_box(stub, tmp_path) -> None:
    """A friend's box must never be told to open the original box's URL."""
    sock, _ = stub
    fake = tmp_path / "bin"
    fake.mkdir()
    write_executable(fake / "tailscale", "#!/bin/sh\nexit 1\n")
    r = _run(sock, tailnet=None, path=f"{fake}:{os.environ['PATH']}")
    assert r.returncode == 0, r.stderr
    assert "<this box>.<your-tailnet>.ts.net" in r.stdout
    assert re.findall(r"[\w-]+\.ts\.net", r.stdout) == [], "no real-looking tailnet host"


def _fake_qrencode(tmp_path: Path) -> str:
    fake = tmp_path / "bin"
    fake.mkdir()
    write_executable(fake / "qrencode", '#!/bin/sh\necho "QR:$*"\n')
    return f"{fake}:{os.environ['PATH']}"


def test_prints_a_qr_code_of_the_url_when_qrencode_is_there(stub, tmp_path) -> None:
    """Typing the long .ts.net URL on a phone was the complaint (2026-10-02)."""
    sock, _ = stub
    r = _run(sock, path=_fake_qrencode(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "QR:-t ansiutf8 -m 2 https://box.example.ts.net:8443/#pair=SECRET-INVITE" in r.stdout


def test_no_qr_code_of_the_placeholder(stub, tmp_path) -> None:
    sock, _ = stub
    path = _fake_qrencode(tmp_path)
    write_executable(tmp_path / "bin" / "tailscale", "#!/bin/sh\nexit 1\n")
    r = _run(sock, tailnet=None, path=path)
    assert r.returncode == 0, r.stderr
    assert "QR:" not in r.stdout


def test_no_qrencode_is_not_an_error(stub, tmp_path) -> None:
    """A box installed before qrencode was added still prints the link."""
    sock, _ = stub
    env_path = ":".join(d for d in os.environ["PATH"].split(":")
                        if not (Path(d) / "qrencode").exists())
    r = _run(sock, path=env_path)
    assert r.returncode == 0, r.stderr
    assert "#pair=SECRET-INVITE" in r.stdout
    assert "Scan this" not in r.stdout
