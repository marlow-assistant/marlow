"""deploy/devices.sh against a local stub of the admin endpoint."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "devices.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("curl") is None, reason="bash and curl required"
)


def _run(sock: str, *args: str):
    env = {"PATH": os.environ["PATH"], "MARLOW_ADMIN_SOCKET": sock}
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env)


def test_list_prints_fingerprint_date_and_name(internal_stub) -> None:
    internal_stub.reply = (200, {"devices": [
        {"device_name": "Pixel", "paired_at": 1_700_000_000.0, "fingerprint": "abcd1234"},
        {"device_name": "iPhone", "paired_at": 1_600_000_000.0, "fingerprint": "00ff00ff"},
    ]})
    r = _run(str(internal_stub.path))
    assert r.returncode == 0, r.stderr
    lines = r.stdout.strip().splitlines()
    assert lines[0].startswith("00ff00ff") and lines[0].endswith("iPhone")  # oldest first
    assert lines[1].startswith("abcd1234") and lines[1].endswith("Pixel")
    assert internal_stub.requests[0][0] == "/internal/admin/devices"


def test_list_with_no_devices_says_so(internal_stub) -> None:
    internal_stub.reply = (200, {"devices": []})
    r = _run(str(internal_stub.path))
    assert r.returncode == 0, r.stderr
    assert "No paired devices" in r.stdout


def test_revoke_sends_the_fingerprint(internal_stub) -> None:
    internal_stub.reply = (200, {"revoked": True})
    r = _run(str(internal_stub.path), "revoke", "abcd1234")
    assert r.returncode == 0, r.stderr
    assert "Revoked abcd1234" in r.stdout
    path, _ctype, body = internal_stub.requests[0]
    assert path == "/internal/admin/devices/revoke"
    assert body == {"fingerprint": "abcd1234"}


def test_revoke_with_no_match_fails(internal_stub) -> None:
    internal_stub.reply = (200, {"revoked": False})
    r = _run(str(internal_stub.path), "revoke", "abcd1234")
    assert r.returncode == 1
    assert "nothing revoked" in r.stderr


@pytest.mark.parametrize("bad", ["", "abc", "ABCD1234", 'abcd1234"}', "$(id)"])
def test_revoke_refuses_anything_but_eight_hex_digits(internal_stub, bad: str) -> None:
    r = _run(str(internal_stub.path), "revoke", bad)
    assert r.returncode == 2
    assert internal_stub.requests == []


def test_fails_clearly_when_nothing_is_listening(tmp_path) -> None:
    r = _run(str(tmp_path / "nothing.sock"))
    assert r.returncode != 0
    assert "marlow-server running" in r.stderr
