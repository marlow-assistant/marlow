"""A second instance on the box (docs/tasks/marlow-for-taylor.md): the unit
and policy update.sh renders for it, install.sh --instance's argument checks,
and the per-instance cert renewal.

The rendering is checked by running update.sh's own code, extracted, since the
script itself needs root and a real box."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
UPDATE = DEPLOY / "update.sh"
INSTALL = DEPLOY / "install.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")


def _render_unit() -> str:
    fn = subprocess.run(
        ["sed", "-n", "/^render_instance_unit()/,/^}/p", str(UPDATE)],
        capture_output=True, text=True, check=True,
    ).stdout
    assert fn.strip(), "render_instance_unit not found in update.sh"
    r = subprocess.run(
        ["bash", "-c", f'{fn}\nrender_instance_unit "$1"', "_", str(DEPLOY / "marlow-server.service")],
        capture_output=True, text=True, check=True,
    )
    return r.stdout


def test_the_instance_unit_is_marlows_with_only_home_and_user_changed() -> None:
    """Rendered, not a second file, so a hardening line added to marlow's unit
    reaches the template too. Undoing the substitutions must give marlow's
    unit back, Description aside."""
    rendered = _render_unit()
    assert "/home/marlow" not in rendered
    assert "User=%i\n" in rendered and "Group=%i\n" in rendered
    assert "ExecStart=/home/%i/.venvs/marlow-server/bin/python -m server.app" in rendered
    rw = next(l for l in rendered.splitlines() if l.startswith("ReadWritePaths="))
    rw_paths = rw.removeprefix("ReadWritePaths=").split()
    assert "/home/%i/.marlow" in rw_paths and "/var/spool/cron/crontabs" in rw_paths

    original = (DEPLOY / "marlow-server.service").read_text().splitlines()
    back = (
        rendered.replace("/home/%i/", "/home/marlow/")
        .replace("User=%i\n", "User=marlow\n")
        .replace("Group=%i\n", "Group=marlow\n")
        .splitlines()
    )
    diffs = [(a, b) for a, b in zip(original, back) if a != b]
    assert len(original) == len(back)
    assert [a for a, _ in diffs] == [l for l in original if l.startswith("Description=")]


def test_the_instance_unit_has_no_stray_specifiers() -> None:
    """systemd expands `%` in directives. The only one the rendering adds is
    %i; any other in a directive line would be a surprise substitution."""
    for line in _render_unit().splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        assert set(re.findall(r"%(.)", line)) <= {"i"}, line


def _policy_render_script() -> str:
    text = UPDATE.read_text()
    m = re.search(r"-H bash -c '(sed [^']*)'", text)
    assert m, "the policy rendering moved; update this test"
    return m.group(1)


def _render_policy(user: str, tmp_path: Path) -> str:
    out = tmp_path / f"{user}.json"
    subprocess.run(
        ["bash", "-c", _policy_render_script(), "_", user, str(DEPLOY / "marlow-settings.json"), str(out)],
        check=True,
    )
    return out.read_text()


def test_marlows_policy_renders_byte_for_byte(tmp_path) -> None:
    assert _render_policy("marlow", tmp_path) == (DEPLOY / "marlow-settings.json").read_text()


def test_an_instance_policy_names_only_its_own_home(tmp_path) -> None:
    """Its denies must cover her secrets, not Mike's, and the hook must run
    from her checkout and venv."""
    rendered = _render_policy("taylor", tmp_path)
    policy = json.loads(rendered)
    assert "/home/marlow" not in rendered
    # `//` is the absolute spelling (a single slash anchors at ~/.claude, see
    # test_marlow_settings.test_path_rules_are_not_single_slash_absolute). The
    # sed's `/home/marlow/` matches inside `//home/marlow/`, so the rewrite has
    # to keep the double slash -- assert it did, rather than the bare path.
    assert "Read(//home/taylor/.marlow/data/**)" in policy["permissions"]["deny"]
    assert "Edit(//home/taylor/.claude/**)" in policy["permissions"]["deny"]
    assert not re.search(r'"(Read|Edit)\(/home/', rendered)
    hook = json.dumps(policy["hooks"])
    assert "cd /home/taylor/marlow-server && /home/taylor/.venvs/marlow-server/bin/python -m hooks.pretooluse" in hook


@pytest.mark.parametrize("user", ["Taylor", "x y", "a;b", "-rf", ""])
def test_update_refuses_a_user_name_that_is_not_plain(user) -> None:
    r = subprocess.run(["bash", str(UPDATE)], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "MARLOW_USER": user})
    assert r.returncode != 0
    if user:  # empty falls back to marlow, then stops at the root check
        assert "not a plain user name" in r.stderr


def _install(*args: str):
    return subprocess.run(["bash", str(INSTALL), "--user-name", "Taylor",
                           "--timezone", "America/Edmonton", "--email", "owner@example.com", *args],
                          capture_output=True, text=True)


@pytest.mark.parametrize(
    "args,why",
    [
        (["--instance", "Taylor", "--public-port", "8444"], "plain Linux user"),
        (["--instance", "tay;lor", "--public-port", "8444"], "plain Linux user"),
        (["--instance", "marlow", "--public-port", "8444"], "first instance"),
        (["--instance", "taylor"], "needs --public-port"),
        (["--instance", "taylor", "--public-port", "80"], "needs --public-port"),
        # Leading zero: bash arithmetic would read it as octal.
        (["--instance", "taylor", "--public-port", "08444"], "needs --public-port"),
        (["--instance", "taylor", "--public-port", "8444\nEnvironment=X=1"], "needs --public-port"),
        (["--instance", "taylor", "--public-port", "8443"], "first instance's"),
        (["--public-port", "8444"], "is for --instance"),
    ],
)
def test_install_refuses_bad_instance_arguments_before_the_root_check(args, why) -> None:
    """They land in a systemd drop-in and a cron line, so a newline or a
    specifier would be syntax there."""
    r = _install(*args)
    assert r.returncode != 0
    assert why in r.stderr
    assert "run with sudo" not in r.stderr


def test_install_accepts_a_plain_instance_and_reaches_the_root_check() -> None:
    r = _install("--instance", "taylor", "--public-port", "8444")
    assert r.returncode != 0
    assert "run with sudo" in r.stderr, r.stderr


def test_an_instance_logs_in_to_its_own_claude_account() -> None:
    """Subscription mode, like the first instance: the login check runs for
    every install, against the instance user's own ~/.claude, and the drop-in
    carries no billing or model switch (docs/tasks/marlow-for-taylor.md)."""
    install = INSTALL.read_text()
    login = install[install.index("# --- 7. Claude login"):install.index("# --- 8. TLS")]
    assert 'as_marlow test -s "$HOME_DIR/.claude/.credentials.json"' in login
    assert "API_KEY" not in install
    dropin = install[install.index('cat > "$DROPIN_DIR/10-instance.conf"'):]
    dropin = dropin[: dropin.index("\nEOF\n")]
    assert "MARLOW_MODEL" not in dropin and "AUTH_MODE" not in dropin


def test_the_cert_template_keeps_the_main_units_hardening() -> None:
    main = (DEPLOY / "marlow-cert-renew.service").read_text().splitlines()
    inst = (DEPLOY / "marlow-cert-renew@.service").read_text().splitlines()
    for key in ("User=", "NoNewPrivileges=", "PrivateTmp=", "ProtectSystem=", "ProtectHome="):
        assert [l for l in main if l.startswith(key)] == [l for l in inst if l.startswith(key)], key
    # Its own root-owned copy, never the first instance's or a checkout.
    assert "ExecStart=/opt/marlow/instances/%i/bin/renew-cert.sh" in inst
    assert "ReadWritePaths=/home/%i/.marlow" in inst
    assert "Environment=MARLOW_TLS_OWNER=%i" in inst
    assert "Environment=MARLOW_SYSTEMD_UNIT=marlow-server@%i" in inst


@pytest.mark.parametrize("owner", ["a;b", "Taylor", "x y"])
def test_renew_cert_refuses_an_owner_that_is_not_plain(owner) -> None:
    """It is chowned to as root."""
    r = subprocess.run(["bash", str(DEPLOY / "renew-cert.sh")], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "MARLOW_TAILNET_NAME": "box.example.ts.net",
                            "MARLOW_TLS_OWNER": owner})
    assert r.returncode != 0
    assert "bad MARLOW_TLS_OWNER" in r.stderr


def test_each_instance_has_its_own_migrations_record() -> None:
    update = UPDATE.read_text()
    assert 'MIGRATIONS_RECORD="/var/lib/marlow/instances/$MARLOW_USER/migrations.applied"' in update


def test_an_instance_update_never_rewrites_marlows_units() -> None:
    """Mike deploys branches to his; hers stays on main. Neither update may
    write a file the other's runs from."""
    update = UPDATE.read_text()
    block = update[update.index('if [[ "$MARLOW_USER" != marlow ]]; then\n    INSTANCE_UNIT='):]
    block = block[: block.index("\nfi\nsystemctl daemon-reload")]
    inst, _, mine = block.partition("\nelse\n")
    assert "/etc/systemd/system/marlow-server.service" not in inst
    assert 'INSTANCE_UNIT="/etc/systemd/system/$UNIT.service"' in inst
    assert "marlow-cert-renew@$MARLOW_USER.$kind" in inst
    assert "/etc/systemd/system/marlow-server.service" in mine
    assert "marlow-server@" not in mine, "marlow's update must not touch an instance's unit"


def test_each_instance_has_its_own_root_owned_copies() -> None:
    update = UPDATE.read_text()
    assert 'OPT_DIR="/opt/marlow/instances/$MARLOW_USER"' in update
    sync = update[update.index('echo "== syncing root-owned cron entrypoint'):update.index('echo "== syncing the permission policy')]
    code = [l for l in sync.splitlines() if l.strip() and not l.lstrip().startswith(("#", "echo"))]
    assert code and all("/opt/marlow" not in l for l in code), "a hardcoded /opt/marlow is shared by every instance"


def test_the_instance_drop_in_points_cron_at_its_own_copy() -> None:
    install = INSTALL.read_text()
    assert "Environment=MARLOW_CRON_ENTRYPOINT=/opt/marlow/instances/$MARLOW_USER/cron/entrypoint.py" in install


def test_an_instance_install_leaves_the_shared_apparmor_profile_and_packages_alone() -> None:
    install = INSTALL.read_text()
    assert 'if [[ -n "$INSTANCE" && -f /etc/apparmor.d/bwrap ]]; then' in install
    assert 'dpkg -s "$pkg"' in install


def test_renew_cert_refuses_to_write_through_a_symlinked_dir() -> None:
    """Root writes the key there, and the owner's agent can write ~/.marlow."""
    text = (DEPLOY / "renew-cert.sh").read_text()
    check = text.index('if [[ -L "$PARENT_DIR" || -L "$CERT_DIR" ]]; then')
    assert check < text.index('mkdir -p "$PARENT_DIR"') < text.index("tailscale cert --cert-file")


# --- the watchdog across the socket upgrade (the Android user's box, Mike's) ---

def _watchdog(tmp_path: Path, *, socket_exists: bool, socket_ok: bool, port_ok: bool):
    """Run watchdog.sh with curl and sudo stubbed: curl answers per flag, and
    sudo records the restart instead of doing it."""
    import socket as socketlib
    import tempfile

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text(
        "#!/bin/bash\n"
        'for a in "$@"; do [[ "$a" == --unix-socket ]] && exec ' + ("true" if socket_ok else "false") + "; done\n"
        "exec " + ("true" if port_ok else "false") + "\n"
    )
    (bin_dir / "sudo").write_text(f'#!/bin/bash\necho "$@" >> {tmp_path}/restarts\n')
    for f in ("curl", "sudo"):
        (bin_dir / f).chmod(0o755)
    sock = Path(tempfile.mkdtemp(prefix="mt-", dir="/tmp")) / "internal.sock"
    if socket_exists:
        s = socketlib.socket(socketlib.AF_UNIX)
        s.bind(str(sock))
    r = subprocess.run(
        ["bash", str(DEPLOY / "watchdog.sh")], capture_output=True, text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "MARLOW_INTERNAL_SOCKET": str(sock)},
    )
    restarted = (tmp_path / "restarts").exists()
    return r, restarted


def test_watchdog_leaves_a_pre_socket_server_alone_mid_update(tmp_path) -> None:
    """update.sh installs the new watchdog before restarting the old server."""
    r, restarted = _watchdog(tmp_path, socket_exists=False, socket_ok=False, port_ok=True)
    assert r.returncode == 0 and not restarted, r.stdout + r.stderr


def test_watchdog_restarts_a_socket_server_that_stops_answering(tmp_path) -> None:
    """The fallback must not mask a real wedge: once the socket exists, the
    old port answering (someone else's server) proves nothing."""
    r, restarted = _watchdog(tmp_path, socket_exists=True, socket_ok=False, port_ok=True)
    assert restarted, r.stdout + r.stderr


def test_watchdog_restarts_when_nothing_answers(tmp_path) -> None:
    r, restarted = _watchdog(tmp_path, socket_exists=False, socket_ok=False, port_ok=False)
    assert restarted, r.stdout + r.stderr
