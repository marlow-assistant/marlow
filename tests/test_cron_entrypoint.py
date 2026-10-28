import json
import shutil
import subprocess
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from cron import entrypoint
from server.config import settings

_REPO_ROOT = Path(entrypoint.__file__).resolve().parent.parent


def _minimal_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    # cron's own env is close to empty (docs/tasks/scheduled-work.md) — no
    # inherited MARLOW_* vars, so these subprocess tests shouldn't inherit
    # this test run's either. MARLOW_DATA_DIR/MARLOW_LOG_DIR are pinned into
    # tmp_path so module import (get_logger() calls settings.ensure_dirs())
    # never touches the real ~/.marlow on whatever machine runs this suite.
    return {
        "PATH": "/usr/bin:/bin",
        "MARLOW_DATA_DIR": str(tmp_path / "data"),
        "MARLOW_LOG_DIR": str(tmp_path / "logs"),
        **extra,
    }


@pytest.fixture
def stub_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(entrypoint, "settings", replace(settings, working_dir=tmp_path))
    return tmp_path


def test_missing_job_file_exits_nonzero_with_path(stub_settings):
    with pytest.raises(SystemExit) as exc_info:
        entrypoint.read_job_prompt("morning-scan")
    message = str(exc_info.value)
    assert "morning-scan" in message
    assert str(stub_settings / "cron" / "morning-scan.md") in message


def test_empty_job_file_exits_nonzero_with_path(stub_settings):
    job_dir = stub_settings / "cron"
    job_dir.mkdir()
    (job_dir / "morning-scan.md").write_text("   \n")

    with pytest.raises(SystemExit) as exc_info:
        entrypoint.read_job_prompt("morning-scan")
    assert str(job_dir / "morning-scan.md") in str(exc_info.value)


def test_present_job_file_returns_exact_contents(stub_settings):
    job_dir = stub_settings / "cron"
    job_dir.mkdir()
    (job_dir / "morning-scan.md").write_text("scan my email and summarize\n")

    assert entrypoint.read_job_prompt("morning-scan") == "scan my email and summarize\n"


def test_post_job_success_returns_turn_id(internal_stub):
    internal_stub.reply = (202, {"turn_id": "abc-123"})

    response = entrypoint.post_job("morning-scan", "do the scan")

    assert response == {"turn_id": "abc-123"}
    [(path, ctype, body)] = internal_stub.requests
    assert path == "/internal/cron/message"
    assert ctype == "application/json"
    assert body["job"] == "morning-scan"
    assert body["text"] == "do the scan"
    assert body["client_msg_id"].startswith("morning-scan-")


def test_post_job_skip_response_returned_as_is(internal_stub):
    # An "attach" job (docs/tasks/nightly-consolidation.md piece 2) with no
    # attended session gets a skip body back instead of a turn_id — post_job
    # must hand that through rather than raising a KeyError, so main() can
    # tell the difference and still exit 0.
    internal_stub.reply = (202, {"status": "skipped", "reason": "no attended session"})

    response = entrypoint.post_job("consolidate", "review today")

    assert response == {"status": "skipped", "reason": "no attended session"}


def test_main_skip_response_exits_zero(stub_settings, monkeypatch):
    job_dir = stub_settings / "cron"
    job_dir.mkdir()
    (job_dir / "consolidate.md").write_text("review today\n")
    monkeypatch.setattr(
        entrypoint, "post_job", lambda job, text: {"status": "skipped", "reason": "no attended session"}
    )

    assert entrypoint.main(["entrypoint.py", "consolidate"]) == 0


def test_post_job_non_2xx_response_exits_nonzero(internal_stub):
    internal_stub.reply = (500, {"detail": "boom"})

    with pytest.raises(SystemExit) as exc_info:
        entrypoint.post_job("morning-scan", "do the scan")
    assert "HTTP 500" in str(exc_info.value)


def test_post_job_with_nothing_listening_exits_nonzero_naming_the_socket():
    settings.internal_socket_path.unlink(missing_ok=True)

    with pytest.raises(SystemExit) as exc_info:
        entrypoint.post_job("morning-scan", "do the scan")
    assert str(settings.internal_socket_path) in str(exc_info.value)


def test_post_job_non_json_reply_exits_nonzero(internal_stub):
    internal_stub.reply = (202, b"<html>not json</html>")

    with pytest.raises(SystemExit) as exc_info:
        entrypoint.post_job("morning-scan", "do the scan")
    assert "not JSON" in str(exc_info.value)


def test_main_missing_job_file_exits_nonzero_without_crashing(stub_settings):
    with pytest.raises(SystemExit):
        entrypoint.main(["entrypoint.py", "morning-scan"])


def test_main_wrong_arg_count_returns_usage_error():
    assert entrypoint.main(["entrypoint.py"]) == 2
    assert entrypoint.main(["entrypoint.py", "a", "b"]) == 2


def _write_schedules(root: Path, entries: list[dict], timezone: str = "UTC") -> None:
    cron_dir = root / "cron"
    cron_dir.mkdir(exist_ok=True)
    (cron_dir / "schedules.json").write_text(
        json.dumps({"timezone": timezone, "entries": entries})
    )


def test_entry_unknown_id_is_silent_noop(stub_settings, monkeypatch):
    _write_schedules(
        stub_settings,
        [{"id": "known", "cron": "0 9 * * *", "notify": "Known reminder"}],
    )
    monkeypatch.setattr(
        entrypoint,
        "post_notification",
        lambda *args, **kwargs: pytest.fail("an unknown id must post nothing"),
        raising=False,
    )

    assert entrypoint.main(["entrypoint.py", "--entry", "missing"]) == 0


def test_recurring_notify_posts_synthetic_turn_without_pruning(stub_settings, monkeypatch):
    _write_schedules(
        stub_settings,
        [{"id": "stretch", "cron": "0 9 * * *", "notify": "Time to stretch"}],
    )
    calls = []
    monkeypatch.setattr(
        entrypoint,
        "post_notification",
        lambda entry_id, text, client_msg_id: calls.append((entry_id, text, client_msg_id))
        or {"turn_id": "turn-1"},
        raising=False,
    )

    assert entrypoint.main(["entrypoint.py", "--entry", "stretch"]) == 0
    assert calls[0][0:2] == ("stretch", "Time to stretch")
    assert calls[0][2].startswith("stretch-")
    stored = json.loads((stub_settings / "cron" / "schedules.json").read_text())
    assert [entry["id"] for entry in stored["entries"]] == ["stretch"]


def test_due_one_off_prompt_posts_then_prunes(stub_settings, monkeypatch):
    due = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M")
    _write_schedules(
        stub_settings,
        [{"id": "research", "at": due, "prompt": "Research heat pump rebates"}],
    )
    calls = []
    monkeypatch.setattr(
        entrypoint,
        "post_inline_prompt",
        lambda entry_id, text, client_msg_id: calls.append((entry_id, text, client_msg_id))
        or {"turn_id": "turn-2"},
    )

    assert entrypoint.main(["entrypoint.py", "--entry", "research"]) == 0
    assert calls[0][0:2] == ("research", "Research heat pump rebates")
    stored = json.loads((stub_settings / "cron" / "schedules.json").read_text())
    assert stored["entries"] == []


def test_stale_one_off_never_fires_in_a_later_year(stub_settings, monkeypatch):
    _write_schedules(
        stub_settings,
        [{"id": "old", "at": "2000-01-01T09:00", "notify": "Do not fire"}],
    )
    monkeypatch.setattr(
        entrypoint,
        "post_notification",
        lambda *args, **kwargs: pytest.fail("a missed one-off must not fire late"),
        raising=False,
    )

    assert entrypoint.main(["entrypoint.py", "--entry", "old"]) == 0
    stored = json.loads((stub_settings / "cron" / "schedules.json").read_text())
    assert stored["entries"] == []


def test_post_notification_targets_the_synthetic_turn_endpoint(internal_stub):
    internal_stub.reply = (202, {"turn_id": "turn-reminder"})

    response = entrypoint.post_notification("costco", "Go to Costco", "costco-2099-08-28-1400")

    assert response == {"turn_id": "turn-reminder"}
    [(path, _ctype, body)] = internal_stub.requests
    assert path == "/internal/cron/notify"
    assert body == {
        "entry": "costco",
        "text": "Go to Costco",
        "client_msg_id": "costco-2099-08-28-1400",
    }


def test_importable_when_installed_standalone_via_marlow_server_repo(tmp_path):
    # Code-review finding: deploy/README.md §6a installs a standalone
    # root-owned COPY of this file at /opt/marlow/cron/entrypoint.py, with no
    # server/ package next to it (unlike the git checkout, where cron/ sits
    # directly under the repo root). MARLOW_SERVER_REPO exists precisely so
    # `import server.config` still resolves from a copy like that. Reproduce
    # that layout for real — copy just this one file into an isolated
    # directory tree with nothing else in it — rather than trusting the
    # fallback path never gets hit.
    standalone_cron_dir = tmp_path / "opt-marlow" / "cron"
    standalone_cron_dir.mkdir(parents=True)
    shutil.copy(Path(entrypoint.__file__), standalone_cron_dir / "entrypoint.py")

    result = subprocess.run(
        [sys.executable, "-S", str(standalone_cron_dir / "entrypoint.py")],
        env=_minimal_env(tmp_path, MARLOW_SERVER_REPO=str(_REPO_ROOT)),
        capture_output=True,
        text=True,
    )
    # Wrong arg count (no job name) -> main()'s own usage error, proving the
    # module imported successfully rather than dying on ModuleNotFoundError.
    assert result.returncode == 2
    assert "ModuleNotFoundError" not in result.stderr
    assert "usage:" in result.stderr


def test_importable_standalone_fails_without_marlow_server_repo(tmp_path):
    # The other half of the same fix: without the env var, a copy installed
    # away from the repo has no fallback and must fail loudly (not silently
    # run with a broken import), so the gap this finding described is real
    # unless MARLOW_SERVER_REPO is actually set on the deploy box.
    standalone_cron_dir = tmp_path / "opt-marlow" / "cron"
    standalone_cron_dir.mkdir(parents=True)
    shutil.copy(Path(entrypoint.__file__), standalone_cron_dir / "entrypoint.py")

    result = subprocess.run(
        [sys.executable, "-S", str(standalone_cron_dir / "entrypoint.py"), "morning-scan"],
        env=_minimal_env(tmp_path),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "ModuleNotFoundError" in result.stderr


def test_importable_via_fallback_when_run_from_repo_checkout(tmp_path):
    # No MARLOW_SERVER_REPO set, invoked from its real location in the repo
    # (cron/ directly under the repo root) — the fallback path must still
    # work for local/dev/test invocation.
    result = subprocess.run(
        [sys.executable, "-S", entrypoint.__file__],
        env=_minimal_env(tmp_path),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "ModuleNotFoundError" not in result.stderr
    assert "usage:" in result.stderr


def test_inline_prompt_is_posted_as_an_entry_not_a_job(internal_stub):
    """`job` selects ATTACHED_SESSION_JOBS/RETIRING_JOBS behaviour server-side
    and an entry id is Marlow's to choose, so the two must not share a field
    (review 2026-08-30)."""
    internal_stub.reply = (202, {"turn_id": "turn-inline"})

    entrypoint.post_inline_prompt("consolidate", "research heat pump rebates", "consolidate-2099-08-30-0900")

    [(path, _ctype, body)] = internal_stub.requests
    assert path == "/internal/cron/message"
    assert body == {
        "entry": "consolidate",
        "text": "research heat pump rebates",
        "client_msg_id": "consolidate-2099-08-30-0900",
    }
    assert "job" not in body


def test_a_one_off_fires_on_its_instant_not_its_wall_clock_string(stub_settings, monkeypatch):
    """The silent-prune bug, from the other side.

    The guard used to compare `entry.at` against the wall clock in the STORE's
    zone. Under a box whose local zone differs, a due entry looked overdue, so
    it pruned itself and notified nobody — no push, no error, nothing on the
    phone (defect 1, 2026-08-30). Comparing instants is what makes a store zone
    that disagrees with the box harmless.
    """
    due = datetime.now(ZoneInfo("Asia/Tokyo"))
    _write_schedules(
        stub_settings,
        [{"id": "teeth", "at": due.strftime("%Y-%m-%dT%H:%M%z"), "notify": "Brush your teeth"}],
        timezone="Asia/Tokyo",
    )
    posted = []
    monkeypatch.setattr(
        entrypoint,
        "post_notification",
        lambda entry_id, text, client_msg_id: posted.append((entry_id, text))
        or {"turn_id": "turn-9"},
    )
    monkeypatch.setattr(entrypoint, "sync_after_turn", lambda path=None: None)

    assert entrypoint.main(["entrypoint.py", "--entry", "teeth"]) == 0
    assert posted == [("teeth", "Brush your teeth")]


def test_a_fired_one_off_re_renders_the_crontab_it_leaves_behind(stub_settings, monkeypatch):
    """Defect 3: the prune alone left a dead, YEARLESS cron line installed."""
    due = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M")
    _write_schedules(stub_settings, [{"id": "spent", "at": due, "notify": "Now"}])
    monkeypatch.setattr(
        entrypoint, "post_notification", lambda *args: {"turn_id": "turn-10"}
    )
    synced = []
    monkeypatch.setattr(entrypoint, "sync_after_turn", lambda path=None: synced.append(path))

    assert entrypoint.main(["entrypoint.py", "--entry", "spent"]) == 0
    assert synced == [stub_settings / "cron" / "schedules.json"]
    stored = json.loads((stub_settings / "cron" / "schedules.json").read_text())
    assert stored["entries"] == []


def test_due_one_off_job_posts_its_prompt_file_then_prunes(stub_settings, monkeypatch):
    """`at`+`job`, allowed 2026-09-06.

    Before that the validator refused the pairing, and _run_entry's fallthrough
    logged "no dispatchable payload" on the strength of that refusal. This is
    the branch that now receives it. Pruning is the whole reason it renders
    through --entry: a cron line has no year field, so a one-off left in the
    crontab fires again next year.
    """
    due = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M")
    (stub_settings / "cron").mkdir(exist_ok=True)
    (stub_settings / "cron" / "cabot-trail-check.md").write_text("Check the trail\n")
    _write_schedules(
        stub_settings,
        [{"id": "cabot-trail-check", "at": due, "job": "cabot-trail-check"}],
    )
    calls = []
    monkeypatch.setattr(
        entrypoint,
        "post_job",
        lambda job, text, client_msg_id: calls.append((job, text, client_msg_id))
        or {"turn_id": "turn-3"},
    )

    assert entrypoint.main(["entrypoint.py", "--entry", "cabot-trail-check"]) == 0
    # post_job, not post_inline_prompt: the server keys attach/retire behaviour
    # off `job`, and a one-off job should behave like its recurring sibling in
    # every way except happening once.
    assert calls[0][0] == "cabot-trail-check"
    assert calls[0][1] == "Check the trail\n"
    # The entry's own client_msg_id, which carries the MINUTE — post_job's
    # f"{job}-{date}" default is right for a daily job and would let a one-off
    # collide with a same-day manual run of the same job.
    assert calls[0][2].startswith("cabot-trail-check-")
    assert len(calls[0][2]) > len("cabot-trail-check-2026-09-06")

    stored = json.loads((stub_settings / "cron" / "schedules.json").read_text())
    assert stored["entries"] == []


def test_a_one_off_job_invoked_early_neither_fires_nor_prunes(stub_settings, monkeypatch):
    """The at-guard runs before dispatch, so it covers jobs like everything
    else — a manual early invocation must leave it scheduled."""
    (stub_settings / "cron").mkdir(exist_ok=True)
    (stub_settings / "cron" / "cabot-trail-check.md").write_text("Check the trail\n")
    _write_schedules(
        stub_settings,
        [{"id": "cabot-trail-check", "at": "2099-09-09T09:00", "job": "cabot-trail-check"}],
    )
    monkeypatch.setattr(
        entrypoint,
        "post_job",
        lambda *args, **kwargs: pytest.fail("an early invocation must post nothing"),
    )

    assert entrypoint.main(["entrypoint.py", "--entry", "cabot-trail-check"]) == 0
    stored = json.loads((stub_settings / "cron" / "schedules.json").read_text())
    assert [entry["id"] for entry in stored["entries"]] == ["cabot-trail-check"]
