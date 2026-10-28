"""deploy/funnel.sh and the dead-man's check, against stubbed tailscale/systemctl
(docs/tasks/funnel-ingress.md, steps 4 and 6)."""
from __future__ import annotations

import http.server
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")


@pytest.fixture
def box(tmp_path):
    """A fake box: stub binaries that log their calls, and a systemd dir."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    for name in ("systemctl", "tailscale"):
        stub = bindir / name
        # TAILSCALE_FAIL_ON: the `funnel --bg` call fails (exit 124 is what
        # `timeout` returns when Tailscale sat waiting for approval).
        # TS_NO_FUNNEL: the tailnet policy doesn't grant this node Funnel.
        # TS_SERVE_JSON: what `serve status --json` reports.
        stub.write_text(
            '#!/bin/bash\n'
            f'echo "{name} $*" >> "{log}"\n'
            'if [[ "$*" == "status --json" ]]; then\n'
            '  if [[ -n "${TS_NO_FUNNEL:-}" ]]; then echo \'{"Self": {"ID": "nTESTNODE", "CapMap": {}}}\';\n'
            '  else echo \'{"Self": {"ID": "nTESTNODE", "CapMap": {"funnel": null}}}\'; fi; exit 0\n'
            'fi\n'
            'if [[ "$*" == "serve status --json" ]]; then echo "${TS_SERVE_JSON:-"{}"}"; exit 0; fi\n'
            'if [[ "$1" == "funnel" && "$*" == *"--bg"* && -n "${TAILSCALE_FAIL_ON:-}" ]]; then exit "$TAILSCALE_FAIL_ON"; fi\n'
            'exit 0\n'
        )
        stub.chmod(0o755)
    # macOS has no `timeout`; the box's coreutils one runs the command, and so does this.
    stub = bindir / "timeout"
    stub.write_text(f'#!/bin/bash\necho "timeout $1" >> "{log}"\nshift\nexec "$@"\n')
    stub.chmod(0o755)
    systemd = tmp_path / "systemd"
    systemd.mkdir()
    return type("Box", (), {"bin": bindir, "log": log, "systemd": systemd, "tmp": tmp_path})


def _run(
    box, action: str, fn: str, *, port: str = "8443", fail: str = "", path_has_tailscale: bool = True,
    granted: bool = True, serve_json: str = "",
):
    script = f'''
set -uo pipefail
UNIT=marlow-server
UNIT_ENV="MARLOW_PUBLIC_PORT={port}"
unit_var() {{ tr ' ' '\\n' <<<"$UNIT_ENV" | sed -n "s/^$1=//p" | tail -1; }}
# The real one is `install -d -o root -g root`, which a test can't be root for.
install() {{ mkdir -p "${{@: -1}}"; }}
FUNNEL_ACTION="{action}"
source "{DEPLOY}/funnel.sh"
{fn}
'''
    env = {
        "PATH": f"{box.bin}:{os.environ['PATH']}" if path_has_tailscale else "/usr/bin:/bin",
        "FUNNEL_SYSTEMD_DIR": str(box.systemd),
    }
    if fail:
        env["TAILSCALE_FAIL_ON"] = fail
    if not granted:
        env["TS_NO_FUNNEL"] = "1"
    if serve_json:
        env["TS_SERVE_JSON"] = serve_json
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)


def _calls(box) -> list[str]:
    return box.log.read_text().splitlines() if box.log.exists() else []


def _dropin(box) -> Path:
    return box.systemd / "marlow-server.service.d" / "30-funnel.conf"


def test_on_binds_loopback_first_then_starts_funnel(box) -> None:
    r = _run(box, "on", "funnel_before_restart; echo RESTART >> " + str(box.log) + "; funnel_after_restart")
    assert r.returncode == 0, r.stderr
    assert "MARLOW_FUNNEL=1" in _dropin(box).read_text()
    calls = _calls(box)
    # Rebind (drop-in + reload) strictly before Funnel; Funnel after the restart.
    assert calls.index("systemctl daemon-reload") < calls.index("RESTART")
    funnel = [c for c in calls if c.startswith("tailscale funnel")]
    assert funnel == ["tailscale funnel --bg --tcp=8443 tcp://127.0.0.1:8443"]
    assert calls.index("RESTART") < calls.index(funnel[0])
    # Bounded: an approval prompt waits rather than fails, with the server on loopback.
    assert calls[calls.index(funnel[0]) - 1] == "timeout 60"


@pytest.mark.parametrize("exit_code", ["1", "124"], ids=["refused", "timed-out-waiting"])
def test_failed_funnel_rolls_back_to_the_tailnet_bind(box, exit_code) -> None:
    r = _run(
        box, "on", "funnel_before_restart; funnel_after_restart", fail=exit_code,
        serve_json='{"AllowFunnel": {"box.ts.net:8443": true}}',
    )
    assert r.returncode != 0
    assert not _dropin(box).exists()
    calls = _calls(box)
    assert calls[-1] == "systemctl restart marlow-server"
    # A half-made forward is taken down before the restart, or tailscaled would
    # still hold <tailnet ip>:8443 and the server couldn't bind it.
    assert calls.index("tailscale funnel --tcp=8443 off") < calls.index("systemctl restart marlow-server")
    assert "tailscale funnel reset" in calls
    assert "back on the tailnet" in r.stderr


def test_preflight_refuses_a_node_without_the_funnel_grant_before_anything_changes(box) -> None:
    """Without the grant `tailscale funnel` prints a link and WAITS, so the old
    order left the server on loopback, unreachable, until someone clicked
    (test VM, 2026-10-24). The preflight catches it before the rebind."""
    r = _run(box, "on", "funnel_preflight", granted=False)
    assert r.returncode != 0
    assert "https://login.tailscale.com/f/funnel?node=nTESTNODE" in r.stderr
    assert "every" in r.stderr and "nodeAttrs" in r.stderr
    assert "Nothing has been changed" in r.stderr
    assert [c for c in _calls(box) if not c.startswith("tailscale status")] == []
    assert not _dropin(box).exists()


def test_preflight_passes_a_node_with_the_grant(box) -> None:
    r = _run(box, "on", "funnel_preflight")
    assert r.returncode == 0, r.stderr
    assert _calls(box) == ["tailscale status --json"]


def test_preflight_does_not_check_the_grant_to_turn_funnel_off(box) -> None:
    r = _run(box, "off", "funnel_preflight", granted=False)
    assert r.returncode == 0, r.stderr


def test_off_stops_funnel_before_rebinding(box) -> None:
    _dropin(box).parent.mkdir(parents=True)
    _dropin(box).write_text("[Service]\nEnvironment=MARLOW_FUNNEL=1\n")
    r = _run(box, "off", "funnel_before_restart")
    assert r.returncode == 0, r.stderr
    assert not _dropin(box).exists()
    calls = _calls(box)
    assert calls[0] == "tailscale funnel --tcp=8443 off"
    assert calls.index("tailscale funnel --tcp=8443 off") < calls.index("systemctl daemon-reload")


def test_off_clears_the_allowfunnel_tailscale_leaves_behind(box) -> None:
    """`funnel --tcp=8443 off` leaves {"AllowFunnel": {"<name>:8443": true}}:
    nothing served, but the next serve handler on 8443 would be public, and
    `funnel status` still says "Funnel on" (test VM, 2026-10-24)."""
    r = _run(box, "off", "funnel_before_restart", serve_json='{"AllowFunnel": {"box.ts.net:8443": true}}')
    assert r.returncode == 0, r.stderr
    calls = _calls(box)
    assert calls.index("tailscale funnel --tcp=8443 off") < calls.index("tailscale funnel reset")


def test_off_leaves_other_serve_config_alone(box) -> None:
    """`reset` wipes the whole serve config, so not when the box has its own."""
    r = _run(
        box, "off", "funnel_before_restart",
        serve_json='{"TCP": {"443": {"HTTPS": true}}, "AllowFunnel": {"box.ts.net:8443": true}}',
    )
    assert r.returncode == 0, r.stderr
    assert "tailscale funnel reset" not in _calls(box)
    assert "other serve config" in r.stderr


def test_off_with_nothing_left_does_not_reset(box) -> None:
    r = _run(box, "off", "funnel_before_restart", serve_json="{}")
    assert r.returncode == 0, r.stderr
    assert "tailscale funnel reset" not in _calls(box)


def test_no_flag_changes_nothing(box) -> None:
    r = _run(box, "", "funnel_preflight; funnel_before_restart; funnel_after_restart")
    assert r.returncode == 0, r.stderr
    assert _calls(box) == []
    assert not _dropin(box).exists()


def test_a_port_funnel_cannot_serve_is_refused_before_anything_changes(box) -> None:
    r = _run(box, "on", "funnel_preflight", port="8444")
    assert r.returncode != 0
    assert "8444" in r.stderr
    assert _calls(box) == []


def test_preflight_needs_the_tailscale_cli(box) -> None:
    r = _run(box, "on", "funnel_preflight", path_has_tailscale=False)
    assert r.returncode != 0
    assert "tailscale" in r.stderr


def test_the_funnel_setting_lives_in_a_dropin_update_sh_never_rewrites() -> None:
    """Updates keep Funnel as it was because nothing but funnel.sh touches it."""
    update = (DEPLOY / "update.sh").read_text()
    code = [l for l in update.splitlines() if not l.lstrip().startswith("#")]
    assert not [l for l in code if "30-funnel" in l]
    assert "funnel_before_restart" in update and "funnel_after_restart" in update
    # Binding happens before the restart, Funnel only after /health passed.
    assert update.index("funnel_before_restart") < update.index('systemctl restart "$UNIT"')
    assert update.index('systemctl restart "$UNIT"') < update.index("funnel_after_restart")


def test_the_preflight_runs_before_update_sh_changes_anything() -> None:
    """A missing Funnel grant is every box's first --funnel, and the refusal says
    "Nothing has been changed". That is only true before the fetch, the merge
    and the deps reinstall; it sat after all three until the 2026-10-24 review."""
    update = (DEPLOY / "update.sh").read_text()
    code = "\n".join(l for l in update.splitlines() if not l.lstrip().startswith("#"))
    preflight = code.index("funnel_preflight || exit 1")
    for step in ("git fetch origin", "== fast-forward merge", "re-exec'ing update.sh", "pip\" install", "VENV_PIP\" install"):
        if step in code:
            assert preflight < code.index(step), step
    # and its port check can read the unit, so unit_var is defined first
    assert code.index("unit_var()") < preflight


def test_install_passes_funnel_through_to_update() -> None:
    install = (DEPLOY / "install.sh").read_text()
    assert "--funnel) FUNNEL_FLAG=(--funnel)" in install
    assert 'update.sh" --branch "$REF" ${FUNNEL_FLAG[@]' in install


# --- the dead-man's check: /health needs a token now --------------------------

class _Health(http.server.BaseHTTPRequestHandler):
    status = 401

    def do_GET(self):
        self.send_response(type(self).status)
        self.send_header("content-length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def health_server():
    handler = type("H", (_Health,), {})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield handler, f"http://127.0.0.1:{server.server_address[1]}/health"
    server.shutdown()


def _check(url: str, tmp_path: Path):
    state = tmp_path / "state"
    notify = tmp_path / "notify.sh"
    notify.write_text(f'#!/bin/bash\necho "$@" >> "{tmp_path}/notified"\n')
    notify.chmod(0o755)
    env = {
        "PATH": os.environ["PATH"],
        "MARLOW_DEADMANS_HEALTH_URL": url,
        "MARLOW_DEADMANS_STATE_FILE": str(state),
        "MARLOW_DEADMANS_NOTIFY_SCRIPT": str(notify),
    }
    r = subprocess.run(["bash", str(DEPLOY / "deadmans" / "check.sh")], capture_output=True, text=True, env=env)
    return r, state


@pytest.mark.parametrize("status", [200, 401])
def test_deadmans_counts_200_and_401_as_up(health_server, tmp_path, status) -> None:
    handler, url = health_server
    handler.status = status
    r, state = _check(url, tmp_path)
    assert r.returncode == 0, r.stderr
    assert state.read_text().split()[1] == "healthy"


@pytest.mark.parametrize("status", [500, 502, 404])
def test_deadmans_does_not_count_server_errors_as_up(health_server, tmp_path, status) -> None:
    handler, url = health_server
    handler.status = status
    _r, state = _check(url, tmp_path)
    assert state.read_text().split()[1] != "healthy"


def test_deadmans_does_not_count_nothing_listening_as_up(tmp_path) -> None:
    _r, state = _check("http://127.0.0.1:9/health", tmp_path)
    assert state.read_text().split()[1] != "healthy"
