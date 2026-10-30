"""The deploy scripts are the shrink-wrap install path — exercise the ones with
real logic (seed-identity.sh) and syntax-check the rest. seed-identity.sh is the
one-time identity hand-off; getting its clobber guard wrong would destroy a
box's diverged memory."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
SEED = DEPLOY / "seed-identity.sh"


def _run(args, working_dir: Path, extra_env=None):
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "HOME": str(working_dir.parent),
        "MARLOW_WORKING_DIR": str(working_dir),
        # deterministic git identity so the script's commit doesn't depend on
        # the runner's global config
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    if extra_env:
        env.update(extra_env)
    # The starter needs a name and zone to fill its placeholders; tests about
    # something else get a default rather than each spelling it out.
    if "--template" in args and "--user-name" not in args:
        args = [*args, "--user-name", "Priya", "--timezone", "Europe/London"]
    return subprocess.run(
        ["bash", str(SEED), *args], capture_output=True, text=True, env=env
    )


def _bare_run(args, working_dir: Path):
    """_run without the default name and zone, for the tests about them."""
    return subprocess.run(
        ["bash", str(SEED), *args],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": str(working_dir.parent),
            "MARLOW_WORKING_DIR": str(working_dir),
            "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_seed_template_fills_every_placeholder(tmp_path) -> None:
    """A friend's install boots from the starter. One unfilled {{USER_NAME}} is an
    assistant addressing its user by a template token; an unfilled {{TIMEZONE}}
    is a schedule store the server rejects whole."""
    wd = tmp_path / "marlow-identity"
    r = _bare_run(["--template", "--user-name", "Ana & Bo/C\\d", "--timezone", "America/Halifax"], wd)
    assert r.returncode == 0, r.stderr
    seeded = [p for p in wd.rglob("*") if p.is_file() and ".git" not in p.parts]
    for path in seeded:
        assert "{{" not in path.read_text(), f"unfilled placeholder in {path}"
    assert "Ana & Bo/C\\d" in (wd / "USER.md").read_text(), "free text survives verbatim"
    assert '"timezone": "America/Halifax"' in (wd / "cron" / "schedules.json").read_text()


# The phrase a fresh USER.md opens its "Everything else" section with. The
# starter CLAUDE.md sends the assistant to FIRST-RUN.md while USER.md still says
# it, and FIRST-RUN.md tells it to replace that line to finish. Nothing reads it
# but the model, so a reword on any one side would silently end onboarding
# before it started, or never.
FIRST_RUN_MARKER = "Not yet known"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_seeded_identity_starts_in_first_run(tmp_path) -> None:
    wd = tmp_path / "marlow-identity"
    assert _run(["--template"], wd).returncode == 0
    everything_else = (wd / "USER.md").read_text().split("## Everything else", 1)[1]
    assert everything_else.strip().startswith(FIRST_RUN_MARKER)
    pointer = (wd / "CLAUDE.md").read_text().split("## First run", 1)[1].split("\n## ", 1)[0]
    assert f'"{FIRST_RUN_MARKER}"' in pointer
    assert "FIRST-RUN.md" in pointer
    assert f'"{FIRST_RUN_MARKER}"' in (wd / "FIRST-RUN.md").read_text(), "seeded, and says what ends it"


def test_first_run_names_only_seeded_jobs() -> None:
    """FIRST-RUN.md's "What runs on its own" step introduces jobs by id. Rename
    one in the seed and the script would quietly stop mentioning it, with
    nothing failing. The check-in is told, not offered, and isn't a store
    entry since the harness took it over (default-infrastructure.md), so the
    step points at its Settings row instead."""
    identity = Path(__file__).resolve().parent.parent / "identity"
    script = (identity / "FIRST-RUN.md").read_text()
    # Up to the next numbered step, whatever its number.
    step = re.split(r"\n\d+\. ", script.split("**What runs on its own.**", 1)[1], maxsplit=1)[0]
    named = set(re.findall(r"^\s+- `([a-z0-9-]+)`", step, re.MULTILINE))
    seed = json.loads((identity / "cron" / "schedules.json").read_text())
    assert named == {"morning", "week-ahead"}
    assert '"Check-ins"' in step
    assert named <= {e["id"] for e in seed["entries"]}


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_seeded_name_lives_only_in_user_md(tmp_path) -> None:
    """Onboarding may correct the name, and every edit outside memory/ costs a
    phone approval. One home means one approval, and nothing left saying the
    old name -- the per-turn context block included (it says "the user")."""
    wd = tmp_path / "marlow-identity"
    r = _bare_run(["--template", "--user-name", "Zebedee", "--timezone", "Europe/London"], wd)
    assert r.returncode == 0, r.stderr
    holders = sorted(
        str(p.relative_to(wd))
        for p in wd.rglob("*")
        if p.is_file() and ".git" not in p.parts and "Zebedee" in p.read_text()
    )
    assert holders == ["USER.md"]


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_seeded_starter_schedule_is_valid_input(tmp_path, monkeypatch) -> None:
    """The starter's store must load, job prompts included: an invalid one would
    freeze every schedule on a fresh box with nothing on the phone to say why."""
    from server.schedules import load

    wd = tmp_path / "marlow-identity"
    assert _run(["--template"], wd).returncode == 0
    schedules = load(wd / "cron" / "schedules.json")
    assert schedules is not None
    # No `ideas`, `consolidate` or `email-flash`: the renderer adds those from "Your day"
    # (docs/tasks/default-infrastructure.md).
    assert {e.id for e in schedules.entries} == {"morning", "week-ahead"}


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
@pytest.mark.parametrize(
    "args",
    [
        ["--template"],
        ["--template", "--user-name", "Priya"],
        ["--template", "--timezone", "Europe/London"],
        ["--template", "--user-name", "Priya", "--timezone", "Mountain Time"],
        ["--template", "--user-name", "Priya", "--timezone", "MST"],
        ["--template", "--user-name", "{{USER_NAME}}", "--timezone", "Europe/London"],
    ],
)
def test_seed_template_refuses_without_a_usable_name_and_zone(tmp_path, args) -> None:
    wd = tmp_path / "marlow-identity"
    r = _bare_run(args, wd)
    assert r.returncode == 2, r.stdout + r.stderr
    assert not (wd / "SOUL.md").exists(), "refused before seeding anything"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_seed_template_populates_and_git_inits(tmp_path) -> None:
    wd = tmp_path / "marlow-identity"
    r = _run(["--template"], wd)
    assert r.returncode == 0, r.stderr
    for f in ("SOUL.md", "CLAUDE.md", "PREFERENCES.md", "USER.md", "FIRST-RUN.md"):
        assert (wd / f).is_file()
    assert (wd / "memory").is_dir()
    assert (wd / ".git").is_dir()  # diverging identity history, versioned locally


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_seed_refuses_to_clobber_without_force(tmp_path) -> None:
    wd = tmp_path / "marlow-identity"
    assert _run(["--template"], wd).returncode == 0
    (wd / "SOUL.md").write_text("# real diverged personality\n")

    second = _run(["--template"], wd)
    assert second.returncode != 0
    assert "already has an identity" in second.stderr
    assert (wd / "SOUL.md").read_text() == "# real diverged personality\n"  # untouched

    forced = _run(["--template", "--force"], wd)
    assert forced.returncode == 0


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_seed_inits_on_main_not_master(tmp_path) -> None:
    """Without an explicit `-b main` this inherits git's default branch name,
    which is how the box ended up on `master` with no remote's default matching
    (docs/tasks/marlow-working-dir-git-policy.md)."""
    wd = tmp_path / "marlow-identity"
    assert _run(["--template"], wd).returncode == 0
    branch = subprocess.run(
        ["git", "-C", str(wd), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    )
    assert branch.stdout.strip() == "main"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_seed_sets_origin_when_asked_and_never_pushes(tmp_path) -> None:
    """`--origin` is the whole fix for a brain that exists on one machine. It sets
    a remote and nothing else: no push, and no credential — pushing is a
    root-owned systemd timer precisely so no push credential sits in the agent's reach."""
    wd = tmp_path / "marlow-identity"
    url = "git@github.com:mikemason/marlow-identity.git"
    result = _run(["--template", "--origin", url], wd)
    assert result.returncode == 0, result.stderr

    remote = subprocess.run(
        ["git", "-C", str(wd), "remote", "get-url", "origin"], capture_output=True, text=True
    )
    assert remote.stdout.strip() == url
    # Nothing was pushed: the local branch has no upstream and no remote refs.
    refs = subprocess.run(
        ["git", "-C", str(wd), "for-each-ref", "refs/remotes"], capture_output=True, text=True
    )
    assert refs.stdout.strip() == ""


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_seed_without_origin_leaves_no_remote(tmp_path) -> None:
    wd = tmp_path / "marlow-identity"
    assert _run(["--template"], wd).returncode == 0
    remotes = subprocess.run(
        ["git", "-C", str(wd), "remote"], capture_output=True, text=True
    )
    assert remotes.stdout.strip() == ""


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_seed_does_not_clobber_an_existing_origin(tmp_path) -> None:
    """Re-running the seeder (or running it with --force) must not silently
    repoint a box's backup at something else."""
    wd = tmp_path / "marlow-identity"
    assert _run(["--template", "--origin", "git@example.com:first.git"], wd).returncode == 0
    assert _run(["--template", "--force", "--origin", "git@example.com:second.git"], wd).returncode == 0
    remote = subprocess.run(
        ["git", "-C", str(wd), "remote", "get-url", "origin"], capture_output=True, text=True
    )
    assert remote.stdout.strip() == "git@example.com:first.git"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_seed_origin_needs_a_url(tmp_path) -> None:
    r = _run(["--template", "--origin"], tmp_path / "marlow-identity")
    assert r.returncode != 0


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_seed_requires_a_source(tmp_path) -> None:
    r = _run([], tmp_path / "marlow-identity")
    assert r.returncode == 2
    assert "pick a source" in r.stderr


def _unit_env() -> dict[str, str]:
    """Environment= lines from the systemd unit, as a dict."""
    env = {}
    for line in (DEPLOY / "marlow-server.service").read_text().splitlines():
        line = line.strip()
        if line.startswith("Environment="):
            key, _, value = line[len("Environment="):].partition("=")
            env[key] = value
    return env


def test_unit_pins_claude_bin_to_an_absolute_path() -> None:
    """systemd's default PATH excludes ~/.local/bin, where the native installer
    puts the CLI, and systemd never reads shell rc files. config.py's bare
    "claude" default would therefore resolve to nothing at runtime: the service
    starts fine, serves /health, then fails on the first real invocation."""
    claude_bin = _unit_env().get("MARLOW_CLAUDE_BIN")
    assert claude_bin, "unit must set MARLOW_CLAUDE_BIN (PATH lookup won't work under systemd)"
    assert claude_bin.startswith("/"), f"must be absolute, got {claude_bin!r}"


def test_unit_disables_the_cli_autoupdater() -> None:
    """ProtectHome=read-only makes ~/.local unwritable, so a self-update would
    fail obscurely. Pinning also keeps the gate #1 CLI contract meaningful —
    version changes should come from update.sh, not from Anthropic shipping."""
    assert _unit_env().get("DISABLE_AUTOUPDATER") == "1"


def test_unit_keeps_no_new_privileges_and_pregrants_crontab_group() -> None:
    """The scheduler invokes Debian's setgid crontab binary from this service.
    NoNewPrivileges suppresses the setgid transition, so the service needs the
    narrow spool-access group before exec rather than disabling the hardening."""
    unit = (DEPLOY / "marlow-server.service").read_text().splitlines()
    assert "NoNewPrivileges=true" in unit
    assert "SupplementaryGroups=crontab" in unit
    read_write_paths = next(line for line in unit if line.startswith("ReadWritePaths="))
    assert "/var/spool/cron/crontabs" in read_write_paths.split()


def test_identity_push_timer_is_installed_before_schedule_owning_server_restarts() -> None:
    service = (DEPLOY / "marlow-identity-push.service").read_text()
    timer = (DEPLOY / "marlow-identity-push.timer").read_text()
    update = (DEPLOY / "update.sh").read_text()

    assert "User=marlow" in service
    assert "ExecStart=/usr/bin/python3 /opt/marlow/bin/identity-push.py" in service
    assert "OnCalendar=*-*-* *:17:00" in timer
    assert "Persistent=true" in timer
    enable = "systemctl enable --now marlow-identity-push.timer"
    restart = 'systemctl restart "$UNIT"'
    assert enable in update
    assert restart in update
    assert update.index(enable) < update.index(restart)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
@pytest.mark.parametrize(
    "script",
    ["seed-identity.sh", "update.sh", "build-web.sh", "pair-device.sh", "devices.sh", "funnel.sh", "watchdog.sh", "resolve-egress-ips.sh",
     "renew-cert.sh", "lib.sh", "deadmans/check.sh", "deadmans/notify.sh",
     "install.sh", "install-voice.sh"],
)
def test_shell_scripts_parse(script: str) -> None:
    path = DEPLOY / script
    if not path.exists():
        pytest.skip(f"{script} not present")
    r = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    assert r.returncode == 0, f"{script} syntax error: {r.stderr}"


def test_no_pipefail_script_pipes_into_grep_q() -> None:
    """`producer | grep -q` under pipefail fails whenever grep exits before
    the producer finishes writing (SIGPIPE). update.sh's branch check refused
    a branch that had --branch 34 times in 100 on a fresh Ubuntu VM. Capture
    the output, then match it."""
    import re

    offenders = [
        f"{p.relative_to(DEPLOY)}:{text.count(chr(10), 0, m.start()) + 1}"
        for p in sorted(DEPLOY.rglob("*.sh"))
        if "pipefail" in (text := p.read_text())
        for m in re.finditer(r"\|\s*grep\s+-\w*q", text)
    ]
    assert not offenders, offenders


# --- update.sh's /health field reporting -----------------------------------
#
# The parse is a sed, not a JSON parser, deliberately: a deploy-verify step
# should not gain a dependency it can fail on. That trade is only safe if the
# sed is pinned, because the failure mode is silence -- a missing field prints
# nothing and looks exactly like a server that predates the field.

UPDATE = DEPLOY / "update.sh"


def _health_field(field: str, body: str) -> str:
    """Run update.sh's own health_field against a body, without running the
    script -- it is `set -e` with a root check and exits immediately."""
    fn = subprocess.run(
        ["sed", "-n", "/^health_field()/,/^}/p", str(UPDATE)],
        capture_output=True, text=True, check=True,
    ).stdout
    assert fn.strip(), "health_field not found in update.sh — did it get renamed?"
    r = subprocess.run(
        ["bash", "-c", f'{fn}\nhealth_field "$1" "$2"', "_", field, body],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_health_field_reads_the_real_compact_payload() -> None:
    # curl gets this on one line, not pretty-printed.
    body = (
        '{"status":"ok","auth_ok":true,"oldest_pending_permission_age":null,'
        '"git_branch":"main","git_commit":"abc1234"}'
    )
    assert _health_field("git_branch", body) == "main"
    assert _health_field("git_commit", body) == "abc1234"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_health_field_handles_a_branch_name_with_slashes() -> None:
    body = '{"git_branch":"feature/health-build-identity","git_commit":"def5678"}'
    assert _health_field("git_branch", body) == "feature/health-build-identity"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_health_field_is_empty_when_the_server_predates_the_fields() -> None:
    """A rollback to a pre-#48 server must print nothing, not crash the verify
    step -- which is why the caller branches on the commit being non-empty."""
    assert _health_field("git_commit", '{"status":"ok","auth_ok":true}') == ""


# --- the crontab migration (deploy/README.md §6a, review 2026-08-30) ---------
#
# server/schedules.py installs the COMPLETE crontab and refuses to run while
# any line it cannot express is still installed. The two non-job lines became
# timers; the job lines became the box's schedules store, seeded once from that
# box's own crontab.


def test_no_box_specific_schedule_ships() -> None:
    """The 2026-08-30 migration seeded one box's store from a file holding that
    box's real jobs and times. Every box has a store now, and a new one gets the
    identity template's (`identity/cron/schedules.json`, via install.sh), so the
    seed only published one user's routine (retired 2026-10-26)."""
    assert not (DEPLOY / "schedules.seed.json").exists()
    assert "schedules.seed" not in (DEPLOY / "update.sh").read_text()


def test_the_trello_sync_is_not_shipped() -> None:
    """A one-box bodge for one user's board, retired from the repo for the open
    source cut (Mike, 2026-10-27); the Trello connector covers the need. The
    box that has it keeps its installed unit, which update.sh no longer touches."""
    assert not list(DEPLOY.glob("marlow-trello-sync.*"))
    for script in ("update.sh", "install.sh", "README.md"):
        assert "trello" not in (DEPLOY / script).read_text().lower(), script


def test_update_installs_the_timer_that_replaces_the_non_job_cron_line() -> None:
    """The line the renderer can never emit, the identity pusher, must have
    somewhere to go before it takes ownership."""
    update = (DEPLOY / "update.sh").read_text()
    unit = "marlow-identity-push.timer"
    assert (DEPLOY / unit).is_file()
    assert f"systemctl enable --now {unit}" in update
    assert "/opt/marlow/bin/identity-push\\.py" in update
    # The removal must not be gated on grep's exit status: grep exits 1 when it
    # filters everything out, which is a legitimate result on a crontab holding
    # only those two lines, and gating on it left them installed silently
    # (/code-review medium, 2026-08-30).
    assert 'if crontab -u "$MARLOW_USER" -l > "$CURRENT_CRONTAB"' in update
    assert 'cmp -s "$CURRENT_CRONTAB" "$MIGRATED_CRONTAB"' in update


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_seeded_rules_import_preferences_on_their_last_line(tmp_path) -> None:
    """docs/tasks/box-upgrades.md. CLAUDE.md is the software's and update.sh
    replaces it; the user's half arrives only through this import. Last, so
    their text comes after the rules it may override. And the CLI expands it
    only when the file is next to CLAUDE.md -- confirmed from a clean shell
    2026-10-05 -- so both must seed into the same directory."""
    wd = tmp_path / "marlow-identity"
    assert _run(["--template"], wd).returncode == 0
    lines = (wd / "CLAUDE.md").read_text().rstrip("\n").splitlines()
    assert lines[-1] == "@PREFERENCES.md"
    assert (wd / "PREFERENCES.md").is_file()


def test_the_installed_rules_carry_no_placeholders() -> None:
    """update.sh copies identity/CLAUDE.md verbatim, with no placeholder pass,
    so the file must have none -- or a converted box would read {{...}}."""
    for name in ("CLAUDE.md", "FIRST-RUN.md"):
        assert "{{" not in (DEPLOY.parent / "identity" / name).read_text(), name


def test_update_replaces_the_rules_only_on_a_converted_box() -> None:
    """PREFERENCES.md is the marker. Without it CLAUDE.md is still partly the
    user's own -- Mike's v1 migration, or a template seed from before the
    split -- and overwriting it would delete their rules."""
    update = (DEPLOY / "update.sh").read_text()
    step = update[update.index("installing the software's own rules") :]
    step = step[: step.index("\nfi\n")]
    assert 'test -f "$WORKING_DIR/PREFERENCES.md"' in step
    assert "for f in CLAUDE.md FIRST-RUN.md; do" in step
    assert "identity/PREFERENCES.md" not in step, "the user's half is never replaced"


def _install_product_file(tmp_path: Path, src: Path, dst: Path):
    """Run update.sh's own install_product_file with `sudo` stubbed to log its
    user and run the rest, as the migration runner tests below do."""
    fn = subprocess.run(
        ["sed", "-n", "/^install_product_file()/,/^}/p", str(UPDATE)],
        capture_output=True, text=True, check=True,
    ).stdout
    assert fn.strip(), "install_product_file not found in update.sh"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    sudo_log = tmp_path / "sudo.log"
    _fake_tool(bin_dir / "sudo", f'echo "$*" >> {sudo_log}\nshift 3\nexec "$@"\n')
    # The box is Linux; macOS's mv has no -T. GNU `mv -fT src dst` is a plain
    # rename(2) onto dst, which replaces a symlink rather than following it --
    # os.replace is the same call, so the stub keeps the semantics under test.
    _fake_tool(
        bin_dir / "mv",
        '[[ "$1" == -fT ]] || { echo "stub mv only does -fT" >&2; exit 2; }\n'
        f'exec {sys.executable} -c "import os, sys; os.replace(sys.argv[1], sys.argv[2])" "$2" "$3"\n',
    )
    r = subprocess.run(
        ["bash", "-c", f'set -euo pipefail\n{fn}\ninstall_product_file "$1" "$2" label', "_", str(src), str(dst)],
        capture_output=True, text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "MARLOW_USER": "marlow"},
    )
    sudo = sudo_log.read_text().splitlines() if sudo_log.exists() else []
    return r, sudo


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_product_files_are_installed_as_marlow_by_rename(tmp_path) -> None:
    """Same symlink primitive as update.sh's as-marlow note, plus one of its own: cp
    onto a planted `CLAUDE.md -> ~/.claude/settings.json` link writes through
    it. `mv -T` replaces the link instead of following it, and the temp comes
    from mktemp because a fixed `.CLAUDE.md.new` could itself be a planted link
    (/code-review high, 2026-10-05)."""
    src = tmp_path / "src.md"
    src.write_text("the software's rules\n")
    wd = tmp_path / "wd"
    wd.mkdir()
    target = tmp_path / "settings.json"
    target.write_text("secret config\n")
    dst = wd / "CLAUDE.md"
    dst.symlink_to(target)

    r, sudo = _install_product_file(tmp_path, src, dst)
    assert r.returncode == 0, r.stderr
    assert "label installed" in r.stdout
    assert not dst.is_symlink() and dst.read_text() == "the software's rules\n"
    assert target.read_text() == "secret config\n", "wrote through the planted link"
    assert oct(dst.stat().st_mode & 0o777) == "0o644"
    assert sorted(p.name for p in wd.iterdir()) == ["CLAUDE.md"], "temp file left behind"
    assert sudo and all(line.startswith("-u marlow -H ") for line in sudo), "a command ran as root"

    r, _ = _install_product_file(tmp_path, src, dst)
    assert "label unchanged" in r.stdout

    # The stub only sees commands that went through sudo, so also check the
    # source: every command in the function, including the ones inside
    # `if`/`&&`/`$(...)`, starts with the sudo wrapper.
    import re
    body = UPDATE.read_text().split("install_product_file() {", 1)[1].split("\n}\n", 1)[0]
    commands = []
    for line in body.splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith(("#", "echo", "fi", "return", "exit", "local ")):
            continue
        line = re.sub(r"^(if !?|&&|then)\s*", "", line)
        line = re.sub(r'^[a-z_]+="\$\(', "", line)
        line = line.removesuffix("; then").removesuffix("\\").strip()
        commands.append(line)
    assert len(commands) >= 6
    for command in commands:
        assert command.startswith('sudo -u "$MARLOW_USER" -H'), f"unwrapped root command: {command!r}"


def test_shipped_job_prompts_match_the_product_owned_paths() -> None:
    """Every prompt update.sh overwrites must be write-denied to the agent, or
    an edit Marlow makes to it is silently lost at the next deploy -- and every
    denied prompt must be one update.sh installs, or it is frozen forever."""
    import re
    from server.boundaries import PRODUCT_OWNED_PATHS
    from server.config import settings
    update = UPDATE.read_text()
    jobs = re.search(r"^SHIPPED_JOBS=\(([^)]*)\)$", update, re.M).group(1).split()
    assert jobs, "SHIPPED_JOBS is empty"
    owned = {p for p in PRODUCT_OWNED_PATHS if p.startswith(str(settings.working_dir / "cron"))}
    assert owned == {str(settings.working_dir / "cron" / f"{j}.md") for j in jobs}
    for job in jobs:
        assert (DEPLOY.parent / "identity" / "cron" / f"{job}.md").is_file(), job


def test_shipped_job_prompts_carry_nothing_personal() -> None:
    """A shipped prompt is installed on every box, so one copied back from
    Mike's (the ideas job was trialled there first) must not take his name or
    his files with it. Personal sources go in each box's PREFERENCES.md."""
    import re
    update = UPDATE.read_text()
    jobs = re.search(r"^SHIPPED_JOBS=\(([^)]*)\)$", update, re.M).group(1).split()
    personal = re.compile(r"\bMike\b|van-board|travel\.md|\b(he|him|his)\b")
    for job in jobs:
        text = (DEPLOY.parent / "identity" / "cron" / f"{job}.md").read_text()
        # The JSON example's `source` names a file to show the field's shape.
        text = text.replace('"source": "travel.md, ', '"source": "')
        found = personal.findall(text)
        assert not found, f"cron/{job}.md: {found}"


def test_shipped_job_prompts_are_installed_before_the_migrations() -> None:
    """A migration that schedules a job is validated against its prompt."""
    update = UPDATE.read_text()
    assert update.index("installing the software's own job prompts") < update.index(
        '\nrun_migrations "$MIGRATIONS_RECORD"'
    )


def test_update_ships_the_permission_policy_not_just_the_code() -> None:
    """The live policy is a COPY at ~marlow/.claude/settings.json.

    Until 2026-09-06 `update.sh` shipped code only and that copy moved by a
    hand-run `cp` documented in a task doc, so every settings change
    half-landed by default. Two known costs: the `<working_dir>/.git` deny
    nearly shipped one-sided (2026-08-08), and a dead `TodoWrite` allow entry
    outlived the tool by five months partly because the live file was never
    looked at.
    """
    update = (DEPLOY / "update.sh").read_text()
    assert (DEPLOY / "marlow-settings.json").is_file()
    assert 'LIVE_POLICY="$CLAUDE_HOME/settings.json"' in update
    assert '"$REPO_DIR/deploy/marlow-settings.json" "$RENDERED_POLICY"' in update
    assert 'mv -fT "$RENDERED_POLICY" "$LIVE_POLICY"' in update


def test_the_policy_is_copied_as_marlow_never_as_root() -> None:
    """Same primitive as update.sh's as-marlow note: marlow owns ~/.claude, so a root
    write through it can be redirected by a symlink the agent plants. Dropping
    to the unprivileged user removes the primitive rather than blacklisting the
    shapes that reach it (/security-review 2026-08-30)."""
    update = (DEPLOY / "update.sh").read_text()
    syncing = update[update.index('CLAUDE_HOME="/home/$MARLOW_USER/.claude"') :]
    syncing = syncing[: syncing.index("\nfi\n")]
    assert "install " not in syncing, "root must not install into a marlow-owned tree"
    for line in syncing.splitlines():
        stripped = line.strip()
        if stripped.startswith(("mkdir", "cp ", "chmod", "install", "cmp ")):
            raise AssertionError(f"unwrapped root write into ~marlow: {stripped!r}")


def test_update_leaves_the_optional_timers_off_a_box_that_never_had_them() -> None:
    """The identity pusher needs a deploy key that a fresh install does not
    have. Enabled unconditionally, every fresh install would start a timer that
    fails on every tick. The enable must sit behind the installed-unit check,
    not merely near it."""
    update = (DEPLOY / "update.sh").read_text()
    for unit in ("marlow-identity-push.timer",):
        enable = f"systemctl enable --now {unit}"
        guard = f'if [[ -f /etc/systemd/system/{unit} ]]; then\n    {enable}'
        assert update.count(enable) == 1
        assert guard in update, f"{unit} is enabled without checking it was installed"


def test_update_assigns_working_dir_before_using_it() -> None:
    """update.sh runs under `set -u`, so the first $WORKING_DIR read must come
    after its assignment. Retiring the schedules seed once took the only
    assignment with it and every update died with 'unbound variable'."""
    update = (DEPLOY / "update.sh").read_text()
    assign = update.find('WORKING_DIR="${MARLOW_WORKING_DIR:-')
    use = update.find('"$WORKING_DIR')
    assert assign != -1, "update.sh never assigns WORKING_DIR"
    assert assign < use, "update.sh reads $WORKING_DIR before assigning it"



# --- the working dir's name ---------------------------------------------------
#
# ~/claude-assistant was the name from before the assistant was called Marlow.
# It became ~/marlow-identity for the open source cut (Mike, 2026-10-27): the
# repo's identity/ seeds it and the identity pusher backs it up, and ~/marlow
# is the folder `git clone` makes for the repo itself.

RENAME = DEPLOY / "rename-working-dir.sh"
# The scripts that know the old name, because each one handles a box that
# still has it. Everything else in deploy/ and server/ names only the new one.
KNOWS_THE_OLD_NAME = {"rename-working-dir.sh", "update.sh", "install.sh"}


def test_the_box_names_the_working_dir_marlow_identity(monkeypatch) -> None:
    from server.config import _default_working_dir

    monkeypatch.delenv("MARLOW_WORKING_DIR", raising=False)
    assert _default_working_dir() == "~/marlow-identity"
    server_unit = (DEPLOY / "marlow-server.service").read_text()
    push_unit = (DEPLOY / "marlow-identity-push.service").read_text()
    assert re.search(r"^ReadWritePaths=.* /home/marlow/marlow-identity( |$)", server_unit, re.M)
    assert re.search(r"^ReadWritePaths=.* /home/marlow/marlow-identity$", push_unit, re.M)
    assert "Environment=MARLOW_WORKING_DIR=/home/marlow/marlow-identity\n" in push_unit
    assert 'WORKING_DIR="${MARLOW_WORKING_DIR:-/home/$MARLOW_USER/marlow-identity}"' in UPDATE.read_text()
    assert 'WORKING_DIR="${MARLOW_WORKING_DIR:-$HOME/marlow-identity}"' in SEED.read_text()
    assert not (DEPLOY / "seed-from-v1.sh").exists()


def test_only_the_migration_paths_know_the_old_working_dir_name() -> None:
    """A path left on the old name fails silently: a unit's ReadWritePaths
    makes the dir read-only, a policy rule stops matching anything."""
    root = DEPLOY.parent
    for path in [*DEPLOY.rglob("*"), *(root / "server").rglob("*.py"), root / "identity" / "CLAUDE.md"]:
        if not path.is_file() or path.name in KNOWS_THE_OLD_NAME:
            continue
        assert "claude-assistant" not in path.read_text(errors="replace"), path
    # The two that know it, only in the step that handles an old box.
    update = UPDATE.read_text()
    start = update.index("== renaming the working dir")
    end = update.index('\necho "== ', start)
    assert "claude-assistant" not in update[:start] + update[end:]
    install = INSTALL.read_text()
    start = install.index('step "seeding the starter identity"')
    end = install.index("\nfi\n", start)
    assert "claude-assistant" not in install[:start] + install[end:]


def _rename(home: Path, *args: str):
    return subprocess.run(
        ["bash", str(RENAME), *args],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(home)},
    )


def _old_box(home: Path) -> Path:
    """A box from before the rename: the dir, its git history, and the CLI's
    session history keyed by the dir's path."""
    old = home / "claude-assistant"
    (old / "memory").mkdir(parents=True)
    (old / "memory" / "note.md").write_text("kept\n")
    (old / ".git").mkdir()
    sessions = home / ".claude" / "projects" / _projects_key(old)
    sessions.mkdir(parents=True)
    (sessions / "s1.jsonl").write_text("{}\n")
    return old


def _projects_key(path: Path) -> str:
    """The CLI names a project's session dir after its path, every
    non-alphanumeric character a dash: /home/marlow/x-y -> -home-marlow-x-y."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_rename_moves_the_dir_and_its_session_history(tmp_path) -> None:
    """The session history must move with the dir. Left behind, --resume and
    --continue find nothing under the new path and every conversation starts
    cold, with no error anywhere."""
    old = _old_box(tmp_path)
    r = _rename(tmp_path)
    assert r.returncode == 0, r.stderr
    new = tmp_path / "marlow-identity"
    assert not old.exists()
    assert (new / "memory" / "note.md").read_text() == "kept\n"
    assert (new / ".git").is_dir()
    projects = tmp_path / ".claude" / "projects"
    assert not (projects / _projects_key(old)).exists()
    assert (projects / _projects_key(new) / "s1.jsonl").is_file()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_rename_lists_the_assistants_files_that_name_the_old_path(tmp_path) -> None:
    """Every spelling: a promoted script is as likely to `cd ~/claude-assistant`
    as to name the absolute path. Listed, never rewritten."""
    old = _old_box(tmp_path)
    (old / "scripts").mkdir()
    (old / "scripts" / "tilde.sh").write_text("cd ~/claude-assistant\n")
    (old / "scripts" / "home.sh").write_text('cd "$HOME/claude-assistant"\n')
    (old / "scripts" / "abs.sh").write_text("cat /home/marlow/claude-assistant/USER.md\n")
    (old / "scripts" / "clean.sh").write_text("cd ~/marlow-identity\n")
    r = _rename(tmp_path)
    assert r.returncode == 0, r.stderr
    for name in ("tilde.sh", "home.sh", "abs.sh"):
        assert name in r.stdout, name
    assert "clean.sh" not in r.stdout
    assert (tmp_path / "marlow-identity" / "scripts" / "tilde.sh").read_text() == "cd ~/claude-assistant\n"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_rename_is_a_no_op_on_a_new_box_and_on_a_rerun(tmp_path) -> None:
    r = _rename(tmp_path)
    assert r.returncode == 0, r.stderr
    assert list(tmp_path.iterdir()) == []

    _old_box(tmp_path)
    assert _rename(tmp_path).returncode == 0
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    r = _rename(tmp_path)
    assert r.returncode == 0, r.stderr
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_rename_without_session_history_still_moves_the_dir(tmp_path) -> None:
    """A box that was seeded and never ran a turn has no projects dir yet."""
    old = _old_box(tmp_path)
    shutil.rmtree(tmp_path / ".claude")
    assert _rename(tmp_path).returncode == 0
    assert not old.exists()
    assert (tmp_path / "marlow-identity" / "memory" / "note.md").is_file()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
@pytest.mark.parametrize("taken", ["dir", "sessions"])
def test_rename_refuses_when_a_target_exists_and_moves_nothing(tmp_path, taken) -> None:
    """Both names present means something already went wrong on this box.
    Merging two identities is a person's call, and a half-done move (dir moved,
    history not) is worse than none, so every target is checked first."""
    old = _old_box(tmp_path)
    new = tmp_path / "marlow-identity"
    if taken == "dir":
        new.mkdir()
    else:
        (tmp_path / ".claude" / "projects" / _projects_key(new)).mkdir()
    for args in ((), ("--dry-run",)):
        r = _rename(tmp_path, *args)
        assert r.returncode == 1, (args, r.stdout)
        assert "marlow-identity" in r.stderr
        assert (old / "memory" / "note.md").is_file()
        assert (tmp_path / ".claude" / "projects" / _projects_key(old) / "s1.jsonl").is_file()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_rename_dry_run_changes_nothing(tmp_path) -> None:
    """update.sh asks first, so a box that would refuse is left running."""
    old = _old_box(tmp_path)
    r = _rename(tmp_path, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert old.is_dir()
    assert not (tmp_path / "marlow-identity").exists()


def test_update_stops_the_server_only_after_the_dry_run_passes() -> None:
    """The move needs the server stopped: it holds the old path from startup,
    and a turn mid-move would write into a dir that is no longer there. The
    dry run comes first, so a box that would refuse keeps the old process
    serving. Before the policy sync, so a refusal leaves the old policy with
    the old dir, and every later step sees the dir at its new name."""
    update = UPDATE.read_text()
    step = update[update.index("== renaming the working dir") :]
    step = step[: step.index("\necho \"== ")]
    assert 'RENAME="$REPO_DIR/deploy/rename-working-dir.sh"' in step
    dry = step.index('bash "$RENAME" --dry-run')
    stop = step.index('systemctl stop "$UNIT"')
    move = step.index('bash "$RENAME" ||')
    assert dry < stop < move
    assert 'sudo -u "$MARLOW_USER" -H' in step[dry - 80 : dry]
    assert "|| exit 1" in step[dry:stop] and "|| exit 1" in step[move:]
    assert 'MARLOW_WORKING_DIR' in step[:dry], "a box that pins its own dir is left alone"
    assert update.index("== renaming the working dir") < update.index("== syncing the permission policy")


def test_install_counts_a_box_on_the_old_name_as_seeded() -> None:
    """install.sh seeds before it hands over to update.sh, which does the move.
    Rerun on a box still on the old name, it must not seed a second, empty
    identity at the new one: the move would then refuse, with two to merge."""
    install = INSTALL.read_text()
    step = install[install.index('step "seeding the starter identity"') :]
    step = step[: step.index("\nfi\n")]
    assert '"$HOME_DIR/marlow-identity/SOUL.md"' in step
    assert '"$HOME_DIR/claude-assistant/SOUL.md"' in step


# --- install.sh ------------------------------------------------------------
#
# The installer needs root and a real Ubuntu box, so what runs here is the part
# that must hold before it touches anything: every argument is validated before
# the root check, so a bad one is refused on any machine, including this one.

INSTALL = DEPLOY / "install.sh"
OK_EMAIL = "priya.o-neil+marlow@mail.example.co.uk"


def _install(*args: str):
    return subprocess.run(["bash", str(INSTALL), *args], capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
@pytest.mark.parametrize(
    "name",
    [
        'Pri"ya',   # would close the systemd Environment="..." quoting
        "Pri\\ya",  # systemd escape
        "50%",      # systemd specifier
        "Priya\nEnvironment=MARLOW_BWRAP_ENABLED=0",  # a second drop-in line
        "1Priya",
        "",
    ],
)
def test_install_refuses_a_name_that_is_not_plain(name) -> None:
    """The name was once written into a systemd drop-in, where one stray newline
    is a second directive -- a name that turns confinement off, for example. It
    now lands only in USER.md, but the refusal costs nothing and keeps a name
    that is really a payload out of the prompt too."""
    r = _install("--user-name", name, "--timezone", "Europe/London", "--email", OK_EMAIL)
    assert r.returncode != 0
    assert "run with sudo" not in r.stderr, "refused by validation, not by the root check"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
@pytest.mark.parametrize(
    "args",
    [
        ["--user-name", "Priya", "--timezone", "Mountain Time", "--email", "priya@example.com"],
        ["--user-name", "Priya", "--timezone", "Europe/London", "--email", "priya@example.com",
         "--ref", "--upload-pack=x"],
        ["--user-name", "Priya", "--timezone", "Europe/London", "--email", "priya@example.com",
         "--repo", "ext::sh -c touch% /tmp/x"],
        # The repo is public: an SSH URL (the old deploy-key path) is refused.
        ["--user-name", "Priya", "--timezone", "Europe/London", "--email", "priya@example.com",
         "--repo", "git@github.com:example-org/marlow.git"],
        ["--user-name", "Priya", "--timezone", "Europe/London", "--email", "priya@example.com", "--bogus"],
        # No --email, and no terminal to ask on: refused, not defaulted.
        ["--user-name", "Priya", "--timezone", "Europe/London"],
        # The address lands in a systemd drop-in: same rule as the name.
        ["--user-name", "Priya", "--timezone", "Europe/London", "--email", "marlow@localhost"],
        ["--user-name", "Priya", "--timezone", "Europe/London", "--email", "a b@example.com"],
        ["--user-name", "Priya", "--timezone", "Europe/London", "--email", "50%@example.com"],
        ["--user-name", "Priya", "--timezone", "Europe/London",
         "--email", "p@example.com\nEnvironment=MARLOW_BWRAP_ENABLED=0"],
    ],
)
def test_install_refuses_bad_arguments_before_the_root_check(args) -> None:
    r = _install(*args)
    assert r.returncode != 0
    assert "run with sudo" not in r.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
@pytest.mark.parametrize("name", ["Priya", "Ana-Maria O'Neil", "Dr. J"])
def test_install_accepts_ordinary_names_and_reaches_the_root_check(name) -> None:
    """The positive half, so the refusals above are not passing merely because
    everything is refused."""
    if subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip() == "0":
        pytest.skip("running as root would start a real install")
    r = _install("--user-name", name, "--timezone", "Europe/London", "--email", OK_EMAIL)
    assert r.returncode == 1
    assert "run with sudo" in r.stderr


def test_install_keeps_the_name_out_of_the_server_config() -> None:
    """USER.md is the name's only home. A second copy the server reads -- the
    MARLOW_USER_NAME drop-in this used to write -- splits from it the first time
    onboarding records a nickname, because the agent can edit USER.md and not a
    systemd file (install-review-leftovers.md item 1)."""
    install = INSTALL.read_text()
    code = "\n".join(l for l in install.splitlines() if not l.lstrip().startswith("#"))
    assert "MARLOW_USER_NAME" not in code
    # The drop-ins it writes are a second instance's settings and the push
    # contact, never the name.
    for name in ("10-instance.conf", "15-push-contact.conf"):
        dropin = install[install.index(f'cat > "$DROPIN_DIR/{name}"'):]
        dropin = dropin[: dropin.index("\nEOF\n")]
        assert "USER_NAME" not in dropin
    assert code.count("service.d") == 1


def test_install_writes_the_push_contact_for_every_instance() -> None:
    """Safari's push service refuses the placeholder contact, so the first
    instance needs the drop-in as much as a second one: it is written outside
    the --instance branch, before update.sh first starts the unit."""
    install = INSTALL.read_text()
    write = install.index('cat > "$DROPIN_DIR/15-push-contact.conf"')
    assert "Environment=MARLOW_WEBPUSH_SUBJECT=mailto:$EMAIL" in install[write:]
    assert write < install.index('if [[ -n "$INSTANCE" ]]; then\n    cat > "$DROPIN_DIR/10-instance.conf"')
    assert write < install.index('"$REPO_DIR/deploy/update.sh"')


def test_install_hands_the_shared_steps_to_update_sh() -> None:
    """An install and an update must not disagree about the entrypoint, the
    permission policy or the unit, so install.sh delegates rather than repeats."""
    install = INSTALL.read_text()
    assert '"$REPO_DIR/deploy/update.sh" --branch "$REF"' in install
    assert "marlow-settings.json" not in install
    assert "/opt/marlow/cron" not in install


def test_install_runs_root_jobs_from_root_owned_copies() -> None:
    """marlow owns its checkout, so a root cron line pointing into it would let
    anything running as marlow choose what root runs next (§6a's rule)."""
    install = INSTALL.read_text()
    cron_lines = [l for l in install.splitlines() if l.startswith("*/2 * * * * root")]
    assert len(cron_lines) == 2, "marlow's line and a second instance's"
    assert "root /opt/marlow/bin/watchdog.sh" in cron_lines[1]
    assert "/opt/marlow/instances/$MARLOW_USER/bin/watchdog.sh" in cron_lines[0]
    for line in cron_lines:
        assert "/home/" not in line


def test_root_never_runs_a_script_from_marlows_checkout() -> None:
    """marlow owns /home/marlow/marlow-server. Every unit or cron line that runs
    as root must point at a root-owned copy, and update.sh must be what keeps
    that copy current, or it runs install-day code forever."""
    cert = (DEPLOY / "marlow-cert-renew.service").read_text()
    exec_line = next(l for l in cert.splitlines() if l.startswith("ExecStart="))
    assert exec_line == "ExecStart=/opt/marlow/bin/renew-cert.sh"
    update = (DEPLOY / "update.sh").read_text()
    for script in ("renew-cert.sh", "watchdog.sh", "lib.sh"):
        assert f'"$REPO_DIR/deploy/{script}" "$OPT_DIR/bin/{script}"' in update
    assert "    OPT_DIR=/opt/marlow\n" in update, "marlow's copies stay where its units point"


def test_install_smoke_test_is_not_a_substring_match() -> None:
    """"OAuth token has expired" contains "ok". A substring check passes the
    exact failure the smoke test exists to catch."""
    install = INSTALL.read_text()
    assert '[[ "$NORMALIZED" != ok ]]' in install
    assert "*ok*" not in install


# --- install.sh over HTTPS (2026-10-28) ----------------------------------------
#
# The repository is public, so a box clones it over HTTPS with no key or token.

def test_install_clones_the_public_repo_over_https_by_default() -> None:
    install = INSTALL.read_text()
    assert 'REPO_URL="https://github.com/marlow-assistant/marlow.git"' in install
    # A wrong URL makes GitHub ask for credentials; git must fail rather than
    # stop at a Username prompt that reads as the installer's own question.
    calls = [l for l in install.splitlines()
             if ("git ls-remote" in l or "git clone" in l) and not l.lstrip().startswith("#")]
    assert any("git clone" in l for l in calls), "the clone line moved"
    assert all("GIT_TERMINAL_PROMPT=0" in l for l in calls), calls


# --- the web client build step (deploy/build-web.sh, called by update.sh) -----
#
# update.sh itself needs root and sudo, so the step lives in build-web.sh and is
# run here against a throwaway git repo with fake `node` / `npm` on PATH. The
# fakes log their argv and the output dir they were told to use, and fail on
# demand. Real npm never runs.

BUILD_WEB = DEPLOY / "build-web.sh"


def _fake_tool(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)


@pytest.fixture
def webrepo(tmp_path):
    """A git repo with a committed web/, plus fake node/npm. Returns a runner."""
    repo = tmp_path / "repo"
    (repo / "web").mkdir(parents=True)
    (repo / "web" / "package.json").write_text("{}\n")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e.com"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)

    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    log = tmp_path / "calls.log"
    _fake_tool(fakebin / "node", 'echo "${FAKE_NODE_VERSION:-v22.1.0}"\n')
    _fake_tool(
        fakebin / "npm",
        'echo "npm $* out=${MARLOW_WEB_OUT_DIR:-}" >> "$FAKE_LOG"\n'
        '[[ "$1" == ci && "${FAKE_NPM_FAIL:-}" == ci ]] && exit 1\n'
        'if [[ "$1" == run ]]; then\n'
        '  echo partial > "$MARLOW_WEB_OUT_DIR/half-written.js"\n'
        '  [[ "${FAKE_NPM_FAIL:-}" == build ]] && exit 1\n'
        '  echo new > "$MARLOW_WEB_OUT_DIR/index.html"\n'
        '  [[ "${FAKE_NO_SW:-}" ]] || echo sw > "$MARLOW_WEB_OUT_DIR/sw.js"\n'
        'fi\n',
    )

    def run_mode(mode, **env):
        full = {
            "PATH": f"{fakebin}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "FAKE_LOG": str(log),
            **env,
        }
        return subprocess.run(
            ["bash", str(BUILD_WEB), mode, str(repo)], capture_output=True, text=True, env=full
        )

    def run(**env):
        """What update.sh does: build, then (if that passed) install."""
        r = run_mode("build", **env)
        return r if r.returncode != 0 else run_mode("install", **env)

    def calls() -> list[str]:
        return log.read_text().splitlines() if log.exists() else []

    def make_dist(content="old"):
        (repo / "web" / "dist").mkdir(exist_ok=True)
        (repo / "web" / "dist" / "index.html").write_text(content)

    def stamp_current():
        tree = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD:web"], capture_output=True, text=True
        ).stdout.strip()
        (repo / "web" / ".dist-stamp").write_text(tree + "\n")

    return type("W", (), dict(repo=repo, run=run, run_mode=run_mode, calls=calls, make_dist=make_dist,
                               stamp_current=stamp_current))


def _leftovers(repo: Path) -> list[str]:
    return sorted(p.name for p in (repo / "web").iterdir() if p.name.startswith((".dist-build-", ".dist-old-", ".dist-staged")))


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_build_runs_when_web_changed_and_swaps_dist_in(webrepo) -> None:
    webrepo.make_dist("old")  # a dist exists, but its stamp predates this web/
    r = webrepo.run()
    assert r.returncode == 0, r.stderr
    calls = webrepo.calls()
    assert calls[0] == "npm ci out="
    assert calls[1].startswith("npm run build out=") and "/web/.dist-build-" in calls[1]
    assert (webrepo.repo / "web" / "dist" / "index.html").read_text() == "new\n"
    assert (webrepo.repo / "web" / "dist" / "sw.js").is_file()
    assert _leftovers(webrepo.repo) == []


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_build_skips_when_web_unchanged_and_dist_exists(webrepo) -> None:
    webrepo.make_dist("old")
    webrepo.stamp_current()
    # Even a node too old to build must not fail an update with nothing to build.
    r = webrepo.run(FAKE_NODE_VERSION="v18.0.0")
    assert r.returncode == 0, r.stderr
    assert webrepo.calls() == []
    assert (webrepo.repo / "web" / "dist" / "index.html").read_text() == "old"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_build_runs_when_dist_missing_even_if_web_unchanged(webrepo) -> None:
    webrepo.stamp_current()
    r = webrepo.run()
    assert r.returncode == 0, r.stderr
    assert len(webrepo.calls()) == 2
    assert (webrepo.repo / "web" / "dist" / "sw.js").is_file()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_build_runs_when_dist_lacks_index_html(webrepo) -> None:
    webrepo.stamp_current()
    (webrepo.repo / "web" / "dist").mkdir()
    assert webrepo.run().returncode == 0
    assert len(webrepo.calls()) == 2


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_a_second_run_after_a_good_build_skips(webrepo) -> None:
    """The stamp, not the merge diff, is what makes the re-exec'd second pass
    (and a re-run after a failed build) behave."""
    assert webrepo.run().returncode == 0
    n = len(webrepo.calls())
    assert webrepo.run().returncode == 0
    assert len(webrepo.calls()) == n


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
@pytest.mark.parametrize("failing", ["ci", "build"])
def test_failed_build_exits_nonzero_keeps_old_dist_and_cleans_up(webrepo, failing) -> None:
    webrepo.make_dist("old")
    r = webrepo.run(FAKE_NPM_FAIL=failing)
    assert r.returncode != 0
    assert "FAILED (web client build)" in r.stderr
    assert (webrepo.repo / "web" / "dist" / "index.html").read_text() == "old"
    assert not (webrepo.repo / "web" / "dist" / "half-written.js").exists()
    assert _leftovers(webrepo.repo) == []
    assert not (webrepo.repo / "web" / ".dist-stamp").exists(), "a failure must not count as built"
    # ...so the retry builds again rather than skipping.
    assert webrepo.run().returncode == 0
    assert (webrepo.repo / "web" / "dist" / "index.html").read_text() == "new\n"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_build_without_sw_js_is_refused_and_not_swapped(webrepo) -> None:
    webrepo.make_dist("old")
    r = webrepo.run(FAKE_NO_SW="1")
    assert r.returncode != 0
    assert "sw.js" in r.stderr
    assert (webrepo.repo / "web" / "dist" / "index.html").read_text() == "old"
    assert _leftovers(webrepo.repo) == []


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
@pytest.mark.parametrize("version", ["v18.19.0", "v20.18.9", "v20.2.0"])
def test_old_node_fails_with_a_clear_message(webrepo, version) -> None:
    webrepo.make_dist("old")
    r = webrepo.run(FAKE_NODE_VERSION=version)
    assert r.returncode != 0
    assert f"node {version[1:]} is too old; need >= 20.19.0" in r.stderr
    assert webrepo.calls() == []
    assert (webrepo.repo / "web" / "dist" / "index.html").read_text() == "old"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
@pytest.mark.parametrize("version", ["v20.19.0", "v22.1.0", "v20.100.0"])
def test_node_at_or_above_the_floor_builds(webrepo, version) -> None:
    assert webrepo.run(FAKE_NODE_VERSION=version).returncode == 0


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_missing_node_fails_with_a_clear_message(tmp_path, webrepo) -> None:
    (tmp_path / "bin" / "node").unlink()
    r = webrepo.run()
    assert r.returncode != 0
    assert "node is not installed" in r.stderr


def test_update_builds_as_marlow_after_pip_and_before_restart() -> None:
    update = UPDATE.read_text()
    step = 'sudo -u "$MARLOW_USER" -H bash "$REPO_DIR/deploy/build-web.sh" build "$REPO_DIR"'
    swap = 'sudo -u "$MARLOW_USER" -H bash "$REPO_DIR/deploy/build-web.sh" install "$REPO_DIR"'
    pip = 'install -q -r "$REPO_DIR/server/requirements.txt"'
    assert step in update
    restart = 'systemctl restart "$UNIT"'
    assert swap in update
    # pip, build (stages only), root installs, swap, restart. The server reads
    # web/dist per request, so the swap must sit next to the restart: a root step
    # failing in between must leave the old dist serving the old process.
    assert (
        update.index(pip)
        < update.index(step)
        < update.index("install -o root -g root -m 755")
        < update.index("systemctl enable --now marlow-identity-push.timer")
        < update.index(swap)
        < update.index(restart)
    )
    between = update[update.index(swap) + len(swap):update.index(restart)]
    assert "install -o" not in between and "systemctl" not in between


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_build_phase_only_stages_and_leaves_live_dist_untouched(webrepo) -> None:
    """A failure between build and restart must leave the old dist serving."""
    webrepo.make_dist("old")
    assert webrepo.run_mode("build").returncode == 0
    web = webrepo.repo / "web"
    assert (web / "dist" / "index.html").read_text() == "old"
    assert (web / ".dist-staged" / "sw.js").is_file()
    assert not (web / ".dist-stamp").exists()
    assert webrepo.run_mode("install").returncode == 0
    assert (web / "dist" / "index.html").read_text() == "new\n"
    assert _leftovers(webrepo.repo) == []
    assert (web / ".dist-stamp").is_file()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_install_with_nothing_staged_is_a_noop(webrepo) -> None:
    webrepo.make_dist("old")
    r = webrepo.run_mode("install")
    assert r.returncode == 0, r.stderr
    assert (webrepo.repo / "web" / "dist" / "index.html").read_text() == "old"
    assert not (webrepo.repo / "web" / ".dist-stamp").exists()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_a_stale_staged_dir_is_discarded_by_the_next_build(webrepo) -> None:
    webrepo.make_dist("old")
    webrepo.stamp_current()
    stale = webrepo.repo / "web" / ".dist-staged"
    stale.mkdir()
    (stale / "index.html").write_text("stale")
    assert webrepo.run_mode("build").returncode == 0  # skips: nothing to build
    assert not stale.exists()
    assert webrepo.run_mode("install").returncode == 0
    assert (webrepo.repo / "web" / "dist" / "index.html").read_text() == "old"


def test_install_installs_node_at_the_web_builds_floor() -> None:
    """update.sh builds the web client and build-web.sh refuses an old node, so
    an installer without node stops every fresh box at that step. The two
    floors are separate constants; this keeps them equal."""
    import re

    install = INSTALL.read_text()
    build = (DEPLOY / "build-web.sh").read_text()
    floor = re.compile(r'^NODE_FLOOR="([0-9.]+)"', re.M)
    assert floor.search(install).group(1) == floor.search(build).group(1)
    assert "apt-get install -y -q nodejs" in install
    # Before update.sh, which is where the build runs.
    assert install.index("apt-get install -y -q nodejs") < install.index('"$REPO_DIR/deploy/update.sh"')


def _install_with_box_zone(zone: str, tmp_path, *args: str):
    """install.sh with no --timezone, on a box whose timedatectl says `zone`."""
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "timedatectl").write_text(f"#!/bin/sh\necho {zone}\n")
    (fake / "timedatectl").chmod(0o755)
    env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}"}
    return subprocess.run(["bash", str(INSTALL), "--user-name", "Priya", "--email", OK_EMAIL, *args],
                          capture_output=True, text=True, env=env)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
@pytest.mark.parametrize("zone", ["Etc/UTC", "UTC"])
def test_install_refuses_a_defaulted_utc_zone(zone, tmp_path) -> None:
    """Cloud images ship in UTC; seeding the starter schedules there puts a
    friend's morning briefing in the middle of their night."""
    r = _install_with_box_zone(zone, tmp_path)
    assert r.returncode != 0
    assert "--timezone" in r.stderr and "run with sudo" not in r.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
@pytest.mark.parametrize(
    "zone, args",
    [("America/Vancouver", ()), ("Etc/UTC", ("--timezone", "UTC"))],
    ids=["a real defaulted zone", "UTC asked for explicitly"],
)
def test_install_accepts_a_real_or_explicit_zone(zone, args, tmp_path) -> None:
    if subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip() == "0":
        pytest.skip("running as root would start a real install")
    r = _install_with_box_zone(zone, tmp_path, *args)
    assert r.returncode == 1
    assert "run with sudo" in r.stderr


def test_install_clones_over_https_and_creates_no_ssh_key() -> None:
    """Root runs code from the clone. With the repo public there is no SSH
    path, so no host key to pin and no key to generate on the box."""
    code = [l for l in INSTALL.read_text().splitlines() if not l.lstrip().startswith("#")]
    assert not [l for l in code if "ssh-keyscan" in l or "ssh-keygen" in l or "known_hosts" in l]


def test_install_prints_the_pairing_link_as_marlow() -> None:
    """It needs no privilege, and it lives in marlow's writable checkout."""
    runs = [l for l in INSTALL.read_text().splitlines()
            if "pair-device.sh" in l and l.rstrip().endswith("|| true")]
    assert runs and all(l.lstrip().startswith("as_marlow ") for l in runs), runs


def test_root_scripts_leave_the_callers_directory_before_running_as_marlow() -> None:
    """sudo -u keeps the cwd; marlow can't enter an admin's 750 home (2026-10-02)."""
    for script in (INSTALL, DEPLOY / "update.sh"):
        lines = [l.strip() for l in script.read_text().splitlines()]
        first_cd = lines.index("cd /")
        first_as_marlow = next(i for i, l in enumerate(lines)
                               if not l.startswith("#")
                               and ("sudo -u" in l or l.startswith("as_marlow ")))
        assert first_cd < first_as_marlow, script.name


# --- the data migration runner (update.sh's run_migrations) -------------------
#
# Run for real against fake migrations: update.sh is root-gated, so the function
# is cut out with sed (as health_field is above) and `sudo` is a stub on PATH
# that logs who it ran as and then runs the rest of the command.


def _run_migrations(tmp_path: Path, migrations: dict[str, str]):
    fn = subprocess.run(
        ["sed", "-n", "/^run_migrations()/,/^}/p", str(UPDATE)],
        capture_output=True, text=True, check=True,
    ).stdout
    assert fn.strip(), "run_migrations not found in update.sh — did it get renamed?"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    sudo_log = tmp_path / "sudo.log"
    _fake_tool(bin_dir / "sudo", f'echo "$*" >> {sudo_log}\nshift 3\nexec "$@"\n')
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir(exist_ok=True)
    for name, body in migrations.items():
        (mig_dir / name).write_text(body)
    record = tmp_path / "state" / "migrations.applied"
    r = subprocess.run(
        ["bash", "-c", f'set -euo pipefail\n{fn}\nrun_migrations "$1" "$2"', "_", str(record), str(mig_dir)],
        capture_output=True, text=True,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "MARLOW_USER": "marlow",
            "VENV_PY": shutil.which("python3") or "/usr/bin/python3",
            "WORKING_DIR": str(tmp_path / "wd"),
            "REPO_DIR": str(tmp_path / "repo"),
        },
    )
    applied = record.read_text().split() if record.exists() else []
    sudo = sudo_log.read_text().splitlines() if sudo_log.exists() else []
    return r, applied, sudo


def _migration(exit_code: int, message: str = "") -> str:
    return f"import sys\nprint({message!r})\nsys.exit({exit_code})\n"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_migrations_run_in_order_once_each_as_marlow(tmp_path) -> None:
    migrations = {"0002-b.py": _migration(0, "b done"), "0001-a.py": _migration(0, "a done")}
    r, applied, sudo = _run_migrations(tmp_path, migrations)
    assert r.returncode == 0, r.stderr
    assert applied == ["0001-a", "0002-b"]
    assert r.stdout.index("0001-a: a done") < r.stdout.index("0002-b: b done")
    assert all(line.startswith("-u marlow -H ") for line in sudo) and len(sudo) == 2
    assert f"--working-dir {tmp_path / 'wd'} --repo {tmp_path / 'repo'}" in sudo[0]

    r, applied, sudo = _run_migrations(tmp_path, migrations)
    assert r.returncode == 0, r.stderr
    assert applied == ["0001-a", "0002-b"], "a second run re-recorded something"
    assert len(sudo) == 2, "a recorded migration ran again"
    assert "nothing new" in r.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_a_deferred_migration_is_retried_and_does_not_block_the_rest(tmp_path) -> None:
    r, applied, _ = _run_migrations(
        tmp_path, {"0001-a.py": _migration(75, "store invalid"), "0002-b.py": _migration(0)}
    )
    assert r.returncode == 0, r.stderr
    assert applied == ["0002-b"]
    assert "0001-a: not recorded, runs again next update" in r.stdout

    r, applied, sudo = _run_migrations(tmp_path, {"0001-a.py": _migration(0, "fixed")})
    assert applied == ["0002-b", "0001-a"]


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_a_failed_migration_stops_the_deploy_unrecorded(tmp_path) -> None:
    r, applied, sudo = _run_migrations(
        tmp_path, {"0001-a.py": "raise SystemExit(2)\n", "0002-b.py": _migration(0)}
    )
    assert r.returncode != 0
    assert applied == []
    assert len(sudo) == 1, "a migration after the failure still ran"
    assert "0001-a failed (exit 2)" in r.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_only_numbered_python_files_are_migrations(tmp_path) -> None:
    r, applied, sudo = _run_migrations(
        tmp_path, {"README.md": "notes", "helper.py": _migration(0), "0001-a.py": _migration(0)}
    )
    assert r.returncode == 0, r.stderr
    assert applied == ["0001-a"] and len(sudo) == 1


def test_the_migration_record_is_root_owned_and_outside_the_working_dir() -> None:
    """The agent runs as marlow and owns the working dir. A record it could
    write would let it re-run, or skip, a migration."""
    update = UPDATE.read_text()
    assert 'MIGRATIONS_RECORD="/var/lib/marlow/migrations.applied"' in update
    # Called in the root body of the script, not under sudo.
    assert '\nrun_migrations "$MIGRATIONS_RECORD" "$REPO_DIR/deploy/migrations"\n' in update


def test_migrations_run_before_the_restart() -> None:
    """Before the restart, because the restarted server renders the crontab
    from the store the migrations edit. The store itself comes from
    install.sh's template seed, which runs before update.sh ever does."""
    update = UPDATE.read_text()
    call = update.index('\nrun_migrations "$MIGRATIONS_RECORD"')
    assert call < update.index('systemctl restart "$UNIT"')
    install = (DEPLOY / "install.sh").read_text()
    assert install.index("seed-identity.sh\" --template") < install.index('deploy/update.sh" --branch')


# --- update.sh's rollback rule ---------------------------------------------
#
# 2026-10-19: a branch cut before #131 merged was deployed over a box that had
# already run 0006-entry-effort, and put the pre-#131 code back without a word.


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"},
    ).stdout.strip()


@pytest.fixture
def history(tmp_path):
    """main: A (0001) -> B (adds 0002). `early` is cut from A, before 0002."""
    repo = tmp_path / "repo"
    (repo / "deploy" / "migrations").mkdir(parents=True)
    _git(tmp_path, "init", "-q", "-b", "main", str(repo))
    (repo / "deploy" / "migrations" / "0001-a.py").write_text("")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "A")
    _git(repo, "branch", "early")
    (repo / "deploy" / "migrations" / "0002-b.py").write_text("")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "B")
    _git(repo, "switch", "-q", "early")
    (repo / "feature.txt").write_text("")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "early feature")
    _git(repo, "switch", "-q", "main")
    return repo


def _rollback_reasons(tmp_path: Path, repo: Path, target: str, applied: list[str] | None) -> list[str]:
    """Run update.sh's own rollback_reasons with `sudo` stubbed out."""
    fn = subprocess.run(
        ["sed", "-n", "/^rollback_reasons()/,/^}/p", str(UPDATE)],
        capture_output=True, text=True, check=True,
    ).stdout
    assert fn.strip(), "rollback_reasons not found in update.sh"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _fake_tool(bin_dir / "sudo", 'shift 3\nexec "$@"\n')
    record = tmp_path / "migrations.applied"
    if applied is not None:
        record.write_text("".join(f"{name}\n" for name in applied))
    r = subprocess.run(
        ["bash", "-c", f'set -euo pipefail\n{fn}\nrollback_reasons "$1" "$2" "$3"', "_",
         str(repo), target, str(record)],
        capture_output=True, text=True,
        env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "MARLOW_USER": "marlow"},
    )
    assert r.returncode == 0, r.stderr
    return r.stdout.splitlines()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_a_forward_deploy_has_no_rollback_reasons(tmp_path, history) -> None:
    _git(history, "switch", "-q", "--detach", "main~1")
    assert _rollback_reasons(tmp_path, history, "main", ["0001-a"]) == []
    # Redeploying what is already checked out is not a rollback either.
    assert _rollback_reasons(tmp_path, history, "HEAD", ["0001-a"]) == []


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_a_branch_cut_before_a_migration_is_a_rollback(tmp_path, history) -> None:
    """The 2026-10-19 case: the box is on main and has run 0002."""
    reasons = _rollback_reasons(tmp_path, history, "early", ["0001-a", "0002-b"])
    assert len(reasons) == 2, reasons
    assert "early does not contain the commit checked out now" in reasons[0]
    assert reasons[1] == "migration 0002-b has run on this box but is not in early"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_a_run_migration_missing_from_the_target_refuses_alone(tmp_path, history) -> None:
    """Checked out at A, so `early` is ahead of HEAD -- but 0002 ran on this
    box at some point (a trial of a branch since abandoned), and `early`'s
    code predates it."""
    _git(history, "switch", "-q", "--detach", "main~1")
    assert _rollback_reasons(tmp_path, history, "early", ["0001-a", "0002-b"]) == [
        "migration 0002-b has run on this box but is not in early"
    ]


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_no_migration_record_still_checks_ancestry(tmp_path, history) -> None:
    """A fresh install has no record yet; the ancestry check still applies."""
    reasons = _rollback_reasons(tmp_path, history, "early", None)
    assert len(reasons) == 1 and "does not contain" in reasons[0]


def test_rollback_is_checked_before_the_checkout_moves() -> None:
    update = UPDATE.read_text()
    check = update.index('enforce_rollback_rule "$(rollback_reasons "$REPO_DIR" "origin/$DEPLOY_REF" "$MIGRATIONS_RECORD")"')
    assert check < update.index("git switch '$DEPLOY_REF'")
    assert check < update.index("git merge --ff-only")
    assert update.index('MIGRATIONS_RECORD="/var/lib/marlow/migrations.applied"') < check


def test_force_is_not_passed_to_the_reexec() -> None:
    """The re-exec runs the TARGET's update.sh. Rolling back to one that
    predates --force would refuse it as a usage error -- after the checkout
    had already moved."""
    update = UPDATE.read_text()
    assert 'exec bash "$REPO_DIR/deploy/update.sh" ${REEXEC_ARGS[@]+"${REEXEC_ARGS[@]}"}' in update
    assert '[[ "$arg" == --force ]] || REEXEC_ARGS+=("$arg")' in update


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_an_unpushed_branch_is_left_to_the_existence_check(tmp_path, history) -> None:
    """Otherwise every applied migration reads as missing from it, under a
    'would take the box backwards' refusal, when the advice is 'push it'."""
    assert _rollback_reasons(tmp_path, history, "no-such-branch", ["0001-a"]) == []


def _enforce(reasons: str, *, force: int, deploy_ref: str = "feature"):
    fn = subprocess.run(
        ["sed", "-n", "/^enforce_rollback_rule()/,/^}/p", str(UPDATE)],
        capture_output=True, text=True, check=True,
    ).stdout
    assert fn.strip(), "enforce_rollback_rule not found in update.sh"
    return subprocess.run(
        ["bash", "-c", f'set -euo pipefail\nFORCE={force}\nDEPLOY_REF={deploy_ref}\n{fn}\n'
         'enforce_rollback_rule "$1"\necho deployed', "_", reasons],
        capture_output=True, text=True,
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_rollback_refuses_without_force_and_deploys_with_it() -> None:
    r = _enforce("origin/feature does not contain the commit checked out now (abc1234)\n"
                 "migration 0002-b has run on this box but is not in origin/feature", force=0)
    assert r.returncode == 1 and "deployed" not in r.stdout
    assert "git merge main" in r.stderr
    # Every reason indented, not just the first (seen on the box 2026-10-19).
    assert "   origin/feature does not contain" in r.stderr
    assert "   migration 0002-b has run" in r.stderr

    r = _enforce("migration 0002-b has run on this box but is not in origin/feature", force=1)
    assert r.returncode == 0 and "deployed" in r.stdout
    assert "--force given" in r.stderr

    r = _enforce("", force=0)
    assert r.returncode == 0 and "deployed" in r.stdout and r.stderr == ""


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_leaving_an_unmerged_branch_for_main_gets_its_own_advice() -> None:
    r = _enforce("origin/main does not contain the commit checked out now (abc1234)", force=0, deploy_ref="main")
    assert r.returncode == 1
    assert "git switch main" not in r.stderr and "merge it first" in r.stderr


def test_a_missing_branch_is_named_before_the_self_deploy_check() -> None:
    """`git show` on a missing ref fails like an old update.sh does, so the
    existence check has to come first or a typo reads as 'predates --branch'."""
    update = UPDATE.read_text()
    block = update.split('echo "== checking $DEPLOY_REF can deploy itself =="', 1)[1]
    assert block.index("does not exist. Push the branch first.") < block.index("git show 'origin/$DEPLOY_REF:deploy/update.sh'")


# --- update.sh's speech-to-text settings, read from the unit ----------------------

def _voice_settings(unit_env: str) -> dict[str, str]:
    """Run update.sh's lines that read the unit's Environment for the speech
    install, with `systemctl show` stubbed to answer `unit_env`."""
    # Two pieces: `unit_var` is defined up top, before the Funnel preflight
    # that needs it, and the voice settings read it further down.
    block = subprocess.run(
        ["sed", "-n",
         "-e", "/^UNIT_ENV=/,/^unit_var()/p",
         "-e", "/^STT_MODEL_SET=/,/^\\[\\[ \"\\$STT_DATA_DIR\"/p", str(UPDATE)],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "unit_var()" in block and "STT_DATA_DIR=" in block, \
        "update.sh's voice settings block moved — update this test"
    assert "git fetch" not in block
    script = (
        'systemctl() { printf "%s\\n" "$UNIT_ENV_STUB"; }\nUNIT=marlow-server\nMARLOW_USER=marlow\n'
        f"{block}\n"
        'echo "model=$STT_MODEL_SET"; echo "threads=$STT_THREADS"; echo "data=$STT_DATA_DIR"'
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env={"PATH": os.environ["PATH"], "UNIT_ENV_STUB": unit_env})
    assert r.returncode == 0, r.stderr
    return dict(line.split("=", 1) for line in r.stdout.splitlines())


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_the_speech_model_goes_to_the_units_data_dir() -> None:
    """The server loads the model from its own MARLOW_DATA_DIR, so the install
    must put it there, not under whatever the deploying shell had."""
    got = _voice_settings("MARLOW_DATA_DIR=/srv/marlow/data MARLOW_STT_THREADS=2")
    assert got == {"model": "", "threads": "2", "data": "/srv/marlow/data"}
    assert _voice_settings("PATH=/usr/bin")["data"] == "/home/marlow/.marlow/data"


# --- update.sh's fetch failure (2026-10-28) ----------------------------------
#
# git's error for a refused credential, or for a wrong URL on a public repo
# ("could not read Username", "Repository not found"), reads like a network
# blip, so the box owner retries and gets nowhere. The hint names the likely
# cause and the fix; these pin which
# errors get which hint, since a wrong guess sends someone to fix the wrong
# thing.

def _fetch_hint(url: str, stderr: str) -> str:
    fn = subprocess.run(
        ["sed", "-n", "/^fetch_failure_hint()/,/^}/p", str(UPDATE)],
        capture_output=True, text=True, check=True,
    ).stdout
    assert fn.strip(), "fetch_failure_hint not found in update.sh — did it get renamed?"
    r = subprocess.run(
        ["bash", "-c", f'{fn}\nfetch_failure_hint "$1" "$2"', "_", url, stderr],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


HTTPS = "https://github.com/example-org/marlow.git"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
@pytest.mark.parametrize("stderr", [
    # A stored token git sent and GitHub refused.
    "remote: Invalid username or token. Password authentication is not supported"
    " for Git operations.\nfatal: Authentication failed for "
    "'https://github.com/example-org/marlow.git/'",
    # No usable credential left, and GIT_TERMINAL_PROMPT=0 stops a prompt.
    "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
    # A private repo answers a token without access as if it didn't exist.
    "remote: Repository not found.\nfatal: repository "
    "'https://github.com/example-org/marlow.git/' not found",
    "fatal: unable to access 'https://github.com/example-org/marlow.git/': "
    "The requested URL returned error: 403",
])
def test_fetch_hint_names_an_expired_token_over_https(stderr) -> None:
    hint = _fetch_hint(HTTPS, stderr)
    assert "token" in hint
    assert "new" in hint and "read-only" in hint
    # Marlow's repo is public, so a refusal is most likely a wrong URL; the
    # token is the private-fork case.
    assert "needs no credentials" in hint
    assert hint.index("origin") < hint.index("token")
    assert "Nothing on this box has changed" in hint


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_fetch_hint_does_not_blame_credentials_for_a_network_failure() -> None:
    stderr = ("fatal: unable to access 'https://github.com/example-org/marlow.git/': "
              "Could not resolve host: github.com")
    hint = _fetch_hint(HTTPS, stderr)
    assert "token" not in hint and "deploy key" not in hint
    assert "Nothing on this box has changed" in hint


def test_update_fetch_cannot_hang_on_a_credential_prompt() -> None:
    """update.sh runs by hand under sudo -u; with a lapsed token git would
    otherwise stop at a Username prompt the admin may not recognise as git's."""
    text = UPDATE.read_text()
    fetch = [l for l in text.splitlines() if "git fetch origin" in l and "sudo -u" in l]
    assert fetch, "the fetch line moved"
    assert all("GIT_TERMINAL_PROMPT=0" in l for l in fetch), fetch


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_fetch_hint_never_prints_a_token_held_in_the_url() -> None:
    url = "https://x-access-token:github_pat_SECRET123@github.com/example-org/marlow.git"
    hint = _fetch_hint(url, "fatal: Authentication failed for 'https://github.com/'")
    assert "SECRET123" not in hint and "x-access-token" not in hint
    assert "github.com/example-org/marlow.git" in hint


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_fetch_hint_with_no_origin_url_goes_by_the_error() -> None:
    hint = _fetch_hint("", "fatal: could not read Username for 'https://github.com': "
                           "terminal prompts disabled")
    assert "token" in hint and "deploy key" not in hint
