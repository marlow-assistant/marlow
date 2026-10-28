"""The dev sandbox's fake `crontab` (scripts/dev/fake_crontab.sh): the server's
real install path must work against it, and dev-server.sh must put it first on
PATH, or the e2e suite reaches the developer's own crontab again."""
from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

from server import schedules

REPO = Path(__file__).resolve().parent.parent
FAKE = REPO / "scripts" / "dev" / "fake_crontab.sh"
DEV_SERVER = REPO / "scripts" / "dev-server.sh"


def _fake_on_path(tmp_path, monkeypatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shutil.copy(FAKE, bin_dir / "crontab")
    (bin_dir / "crontab").chmod((bin_dir / "crontab").stat().st_mode | stat.S_IXUSR)
    table = tmp_path / "crontab"
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("MARLOW_DEV_CRONTAB_FILE", str(table))

    # conftest refuses every crontab call; this test exercises install() for
    # real, so it lets one through, and only when `crontab` is the fake.
    def run(argv, **kwargs):
        resolved = shutil.which(argv[0], path=kwargs.get("env", {}).get("PATH"))
        assert resolved == str(bin_dir / "crontab"), f"would run the real crontab: {resolved}"
        return subprocess.run(argv, **kwargs)

    monkeypatch.setattr(
        schedules, "subprocess", SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired)
    )
    return table


def test_install_writes_the_sandbox_table_and_is_idempotent(tmp_path, monkeypatch):
    table = _fake_on_path(tmp_path, monkeypatch)
    rendered = schedules.render(None, [])
    # No table yet: the fake's "no crontab for" must read as empty, not as a
    # failure, or install() refuses and nothing is ever written.
    assert schedules.install(rendered) is True
    assert table.read_text() == rendered
    mtime = table.stat().st_mtime_ns
    assert schedules.install(rendered) is True
    assert table.stat().st_mtime_ns == mtime, "an unchanged table is not rewritten"


def test_the_fake_refuses_without_a_table_path(tmp_path):
    result = subprocess.run(
        ["bash", str(FAKE), "-l"], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin"}, check=False,
    )
    assert result.returncode != 0
    assert "MARLOW_DEV_CRONTAB_FILE" in result.stderr


def test_dev_server_puts_the_fake_first_on_path_before_starting():
    script = DEV_SERVER.read_text()
    install = script.index('fake_crontab.sh" "$DEV/bin/crontab"')
    path = script.index('export PATH="$DEV/bin:$PATH"')
    table = script.index('export MARLOW_DEV_CRONTAB_FILE="$DEV/crontab"')
    start = script.index('exec "$PY" -m server.app')
    assert install < start and path < start and table < start
