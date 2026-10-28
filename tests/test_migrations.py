"""deploy/migrations/NNNN-*.py -- run-once data migrations for existing boxes.

The runner in update.sh records a migration after it exits 0 and never runs it
again, so the exit codes and the "leave the user's store alone" paths are the
behaviour worth pinning. The invariant test at the bottom is the load-bearing
one: it is what lets a fresh install skip any "mark as applied" step.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from server import schedules
from server.preferences import PreferencesStore
from tests.test_deploy import _run

REPO = Path(__file__).resolve().parent.parent
# The runner's own pattern (update.sh): _store.py is a helper, not a migration.
MIGRATIONS = sorted((REPO / "deploy" / "migrations").glob("[0-9][0-9][0-9][0-9]-*.py"))
EMAIL_FLASH = REPO / "deploy" / "migrations" / "0001-email-flash.py"
EVENING = REPO / "deploy" / "migrations" / "0002-email-flash-evening.py"
IDEAS = REPO / "deploy" / "migrations" / "0003-ideas.py"


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(f"migration_{path.stem.replace('-', '_')}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migrate(path: Path, working_dir: Path) -> int:
    return _load(path).main(["--working-dir", str(working_dir), "--repo", str(REPO)])


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.parts
    }


def _working_dir(tmp_path: Path, entries: list[dict], prompts=("morning", "digest", "email-flash")) -> Path:
    wd = tmp_path / "marlow-identity"
    (wd / "cron").mkdir(parents=True)
    for name in prompts:
        (wd / "cron" / f"{name}.md").write_text(f"{name} prompt\n")
    store = {"timezone": "America/Edmonton", "entries": entries}
    (wd / "cron" / "schedules.json").write_text(json.dumps(store, indent=2) + "\n")
    return wd


def _morning(cron: str) -> dict:
    return {"id": "morning", "cron": cron, "job": "morning"}


def _entries(wd: Path) -> list[dict]:
    return json.loads((wd / "cron" / "schedules.json").read_text())["entries"]


# --- 0001-email-flash and 0002-email-flash-evening -----------------------------
#
# Their behaviour tests were retired with step 3 of default-infrastructure.md:
# all either ever did was add or retime an `email-flash` entry, which the
# validator now refuses because the harness renders the check-in itself. Both
# already ran on every box; on a fresh one 0001's addition is refused as
# invalid and nothing is written, which the fresh-seed invariant still checks.


def _store_at_firing_cap() -> list[dict]:
    # 92 + 1 + 3 = 96 firings: legal on its own, one flash entry (3 more) over.
    assert schedules.MAX_DAILY_FIRINGS == 96, "re-derive this fixture from the new cap"
    return [
        {"id": "busy", "cron": "*/15 0-22 * * *", "prompt": "poll"},
        _morning("0 8 * * *"),
        {"id": "digest", "cron": "0 1,2,3 * * *", "job": "digest"},
    ]


def test_there_is_at_least_one_migration() -> None:
    # The parametrised invariant below passes vacuously on an empty glob.
    assert EMAIL_FLASH in MIGRATIONS
    assert EVENING in MIGRATIONS
    assert IDEAS in MIGRATIONS


# --- 0003-ideas ----------------------------------------------------------------
#
# Its behaviour tests were retired with step 2 of default-infrastructure.md:
# all it ever did was add a `job: ideas` entry, which the validator now refuses
# because the harness renders `ideas` itself. It already ran on every box; on a
# fresh one it finds no consolidate entry and does nothing, which the
# fresh-seed invariant below still checks.


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
@pytest.mark.parametrize("migration", MIGRATIONS, ids=lambda p: p.name)
def test_every_migration_is_a_noop_on_a_fresh_template_seed(tmp_path, migration) -> None:
    """A fresh install already has every migration's effects from the seed.

    That is what lets install skip any "mark as applied" step: if a migration
    changed a freshly seeded box, adding a feature to the seed without also
    updating its migration would double-apply it on the next deploy.
    """
    wd = tmp_path / "marlow-identity"
    r = _run(["--template", "--user-name", "Test", "--timezone", "America/Edmonton"], wd)
    assert r.returncode == 0, r.stderr
    before = _snapshot(wd)
    assert _migrate(migration, wd) == 0
    assert _snapshot(wd) == before


# --- 0004-morning-weather ----------------------------------------------------------

MORNING_WEATHER = REPO / "deploy" / "migrations" / "0004-morning-weather.py"

_USER_BRIEF = """Good morning.

## 1. Schedule
Calendar things.

## 2. Van
Van things.

---

Keep it tight.
"""


def _brief(tmp_path: Path, text: str) -> Path:
    wd = tmp_path / "marlow-identity"
    (wd / "cron").mkdir(parents=True)
    (wd / "cron" / "morning.md").write_text(text)
    return wd


def test_weather_goes_in_before_the_closing_rule_and_nothing_else_moves(tmp_path, capsys) -> None:
    wd = _brief(tmp_path, _USER_BRIEF)
    assert _migrate(MORNING_WEATHER, wd) == 0
    after = (wd / "cron" / "morning.md").read_text()
    assert after == _USER_BRIEF.replace(
        "Van things.\n\n---",
        "Van things.\n\n" + _load(MORNING_WEATHER).SECTION + "\n---",
    )
    assert "added a Weather section" in capsys.readouterr().out


def test_weather_is_appended_when_there_is_no_rule(tmp_path) -> None:
    wd = _brief(tmp_path, "## 1. Schedule\nCalendar things.\n")
    assert _migrate(MORNING_WEATHER, wd) == 0
    assert (wd / "cron" / "morning.md").read_text() == (
        "## 1. Schedule\nCalendar things.\n\n" + _load(MORNING_WEATHER).SECTION
    )


@pytest.mark.parametrize("mention", ["Check the WEATHER first.", "Add a forecast line."])
def test_a_brief_that_already_mentions_weather_is_left_alone(tmp_path, capsys, mention) -> None:
    wd = _brief(tmp_path, _USER_BRIEF + mention + "\n")
    before = _snapshot(wd)
    assert _migrate(MORNING_WEATHER, wd) == 0
    assert _snapshot(wd) == before
    assert "already mentions weather" in capsys.readouterr().out


def test_no_brief_is_done_not_deferred(tmp_path) -> None:
    wd = tmp_path / "marlow-identity"
    (wd / "cron").mkdir(parents=True)
    assert _migrate(MORNING_WEATHER, wd) == 0
    assert not (wd / "cron" / "morning.md").exists()


def test_morning_weather_second_run_is_a_noop(tmp_path) -> None:
    wd = _brief(tmp_path, _USER_BRIEF)
    _migrate(MORNING_WEATHER, wd)
    after_first = _snapshot(wd)
    assert _migrate(MORNING_WEATHER, wd) == 0
    assert _snapshot(wd) == after_first


# --- 0005-week-ahead ---------------------------------------------------------------

WEEK_AHEAD = REPO / "deploy" / "migrations" / "0005-week-ahead.py"
_WEEK_AHEAD_TEMPLATE = (REPO / "identity" / "cron" / "week-ahead.md").read_text()


def test_week_ahead_is_added_with_its_starter_prompt(tmp_path, capsys) -> None:
    wd = _working_dir(tmp_path, [_morning("0 8 * * *")])
    assert _migrate(WEEK_AHEAD, wd) == 0
    assert _entries(wd)[-1] == {"id": "week-ahead", "cron": "0 18 * * 0", "job": "week-ahead"}
    assert (wd / "cron" / "week-ahead.md").read_text() == _WEEK_AHEAD_TEMPLATE
    assert schedules.load(wd / "cron" / "schedules.json") is not None
    assert "added week-ahead" in capsys.readouterr().out


def test_week_ahead_matches_the_seed(tmp_path) -> None:
    wd = _working_dir(tmp_path, [_morning("0 8 * * *")])
    assert _migrate(WEEK_AHEAD, wd) == 0
    seed = json.loads((REPO / "identity" / "cron" / "schedules.json").read_text())
    assert [e for e in _entries(wd) if e["id"] == "week-ahead"] == [
        e for e in seed["entries"] if e["id"] == "week-ahead"
    ]


def test_week_ahead_keeps_a_users_own_prompt(tmp_path) -> None:
    wd = _working_dir(tmp_path, [_morning("0 8 * * *")], prompts=("morning", "week-ahead"))
    assert _migrate(WEEK_AHEAD, wd) == 0
    assert (wd / "cron" / "week-ahead.md").read_text() == "week-ahead prompt\n"
    assert _entries(wd)[-1]["id"] == "week-ahead"


@pytest.mark.parametrize(
    "entries",
    [
        # Mike's box: a hand-made Sunday-evening digest.
        [_morning("30 7 * * *"), {"id": "weekly-digest", "cron": "0 18 * * 0", "job": "morning"}],
        [{"id": "sunday", "cron": "0 9 * * 7", "notify": "x"}],
        [{"id": "sunday", "cron": "0 9 * * 1,0", "notify": "x"}],
        [{"id": "week-ahead", "cron": "0 9 * * 6", "job": "morning"}],
        [{"id": "weekly", "cron": "0 9 * * 6", "job": "week-ahead"}],
    ],
    ids=["mikes-box", "seven", "named-in-list", "by-id", "by-job"],
)
def test_week_ahead_leaves_the_store_alone(tmp_path, entries) -> None:
    wd = _working_dir(tmp_path, entries, prompts=("morning", "week-ahead"))
    before = _snapshot(wd)
    assert _migrate(WEEK_AHEAD, wd) == 0
    assert _snapshot(wd) == before


def test_week_ahead_defers_on_a_missing_or_invalid_store(tmp_path) -> None:
    wd = tmp_path / "marlow-identity"
    (wd / "cron").mkdir(parents=True)
    assert _migrate(WEEK_AHEAD, wd) == 75
    (wd / "cron" / "schedules.json").write_text("{not json")
    assert _migrate(WEEK_AHEAD, wd) == 75
    assert sorted(p.name for p in (wd / "cron").iterdir()) == ["schedules.json"]


def test_week_ahead_is_not_written_over_the_firing_cap(tmp_path, capsys) -> None:
    wd = _working_dir(tmp_path, _store_at_firing_cap())
    assert schedules.load(wd / "cron" / "schedules.json") is not None
    before = _snapshot(wd)
    assert _migrate(WEEK_AHEAD, wd) == 0
    assert _snapshot(wd) == before, "prompt copied in for the attempt must be removed"
    assert "would be rejected" in capsys.readouterr().out


def test_week_ahead_second_run_is_a_noop(tmp_path, capsys) -> None:
    wd = _working_dir(tmp_path, [_morning("0 8 * * *")])
    assert _migrate(WEEK_AHEAD, wd) == 0
    after_first = _snapshot(wd)
    capsys.readouterr()
    assert _migrate(WEEK_AHEAD, wd) == 0
    assert _snapshot(wd) == after_first
    assert "already scheduled" in capsys.readouterr().out


# --- 0006-entry-effort -------------------------------------------------------------

ENTRY_EFFORT = REPO / "deploy" / "migrations" / "0006-entry-effort.py"


def _mikes_store() -> list[dict]:
    return [
        _morning("30 7 * * *"),
        {"id": "weekly-digest", "cron": "0 18 * * 0", "job": "weekly-digest"},
        {"id": "alberta", "cron": "33 7 * * 1", "job": "alberta-weekly"},
    ]


def _effort_dir(tmp_path: Path, entries: list[dict]) -> Path:
    return _working_dir(tmp_path, entries, prompts=("morning", "weekly-digest", "alberta-weekly"))


def test_entry_effort_moves_high_onto_both_hand_made_jobs(tmp_path, capsys) -> None:
    wd = _effort_dir(tmp_path, _mikes_store())
    assert _migrate(ENTRY_EFFORT, wd) == 0
    efforts = {e["id"]: e.get("effort") for e in _entries(wd)}
    assert efforts == {"morning": None, "weekly-digest": "high", "alberta": "high"}
    assert schedules.load(wd / "cron" / "schedules.json") is not None
    assert "weekly-digest, alberta" in capsys.readouterr().out


def test_entry_effort_leaves_an_effort_already_chosen(tmp_path) -> None:
    entries = _mikes_store()
    entries[1]["effort"] = "medium"
    wd = _effort_dir(tmp_path, entries)
    assert _migrate(ENTRY_EFFORT, wd) == 0
    assert [e.get("effort") for e in _entries(wd)] == [None, "medium", "high"]


def test_entry_effort_leaves_a_box_without_those_jobs_alone(tmp_path, capsys) -> None:
    wd = _working_dir(tmp_path, [_morning("0 8 * * *")])
    before = _snapshot(wd)
    assert _migrate(ENTRY_EFFORT, wd) == 0
    assert _snapshot(wd) == before
    assert "nothing to move" in capsys.readouterr().out


def test_entry_effort_defers_on_a_missing_or_invalid_store(tmp_path) -> None:
    wd = tmp_path / "marlow-identity"
    (wd / "cron").mkdir(parents=True)
    assert _migrate(ENTRY_EFFORT, wd) == 75
    (wd / "cron" / "schedules.json").write_text("{not json")
    assert _migrate(ENTRY_EFFORT, wd) == 75


def test_entry_effort_second_run_is_a_noop(tmp_path, capsys) -> None:
    wd = _effort_dir(tmp_path, _mikes_store())
    assert _migrate(ENTRY_EFFORT, wd) == 0
    after_first = _snapshot(wd)
    capsys.readouterr()
    assert _migrate(ENTRY_EFFORT, wd) == 0
    assert _snapshot(wd) == after_first
    assert "nothing to move" in capsys.readouterr().out


# --- 0007-default-infrastructure ----------------------------------------------

HANDOVER = REPO / "deploy" / "migrations" / "0007-default-infrastructure.py"


def _handover(wd: Path, prefs: Path) -> int:
    return _load(HANDOVER).main(
        ["--working-dir", str(wd), "--repo", str(REPO), "--preferences", str(prefs)]
    )


def _overnight(ideas: str = "30 0 * * *", consolidate: str = "0 1 * * *") -> list[dict]:
    return [
        {"id": "ideas", "cron": ideas, "job": "ideas"},
        {"id": "consolidate", "cron": consolidate, "job": "consolidate"},
    ]


def _handover_dir(tmp_path: Path, entries: list[dict]) -> Path:
    return _working_dir(tmp_path, entries, prompts=("morning", "ideas", "consolidate", "email-flash"))


def _prefs(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def _flash(cron: str = "0 11,14,17,20 * * *") -> dict:
    return {"id": "email-flash", "cron": cron, "job": "email-flash"}


def test_handover_removes_the_harness_entries_and_keeps_the_rest(tmp_path, capsys) -> None:
    weekly = {"id": "week-ahead", "cron": "0 18 * * 0", "prompt": "the week"}
    wd = _handover_dir(tmp_path, [*_overnight(), _morning("0 8 * * *"), _flash(), weekly])
    prefs = tmp_path / "preferences.json"
    assert _handover(wd, prefs) == 0
    assert _entries(wd) == [_morning("0 8 * * *"), weekly]
    assert schedules.load(wd / "cron" / "schedules.json") is not None
    out = capsys.readouterr().out
    assert "removed 'ideas' (job 'ideas', was '30 0 * * *')" in out
    assert "removed 'consolidate' (job 'consolidate', was '0 1 * * *')" in out
    assert "removed 'email-flash' (job 'email-flash', was '0 11,14,17,20 * * *')" in out
    # 08:00-23:00 every 3h is the defaults' own schedule: nothing to write.
    assert not prefs.exists()


def test_mikes_box_moves_nothing(tmp_path, system_zone) -> None:
    """His live store on 2026-10-21. After the handover, the harness renders
    the same times the entries did."""
    system_zone("America/Regina")
    entries = [*_overnight(), _morning("30 7 * * *"), _flash("30 10,13,16,19 * * *")]
    wd = _handover_dir(tmp_path, entries)
    prefs = tmp_path / "preferences.json"
    assert _handover(wd, prefs) == 0
    assert _prefs(prefs) == {"day_start": "07:30", "day_end": "23:00"}
    store = PreferencesStore(prefs)
    assert schedules.harness_crons(store) == [
        ("ideas", "30 0 * * *"), ("consolidate", "0 1 * * *"), ("email-flash", "30 10,13,16,19 * * *"),
    ]


@pytest.mark.parametrize(
    "flash, paired, expected",
    [
        ("0 10,12,14,16,18,20 * * *", True, 2),
        ("0 9,13,17 * * *", True, 4),
        ("0 11,14,20 * * *", True, None),  # uneven: the default
        ("0 11,14,17,20 * * 1-5", True, None),  # weekdays only: the default
        (None, True, 0),  # deleted on a used box: stays off
        (None, False, None),  # a fresh install: the default
    ],
)
def test_handover_seeds_the_checkin_interval(tmp_path, flash, paired, expected) -> None:
    entries = [*_overnight(), _morning("0 8 * * *")] + ([_flash(flash)] if flash else [])
    wd = _handover_dir(tmp_path, entries)
    prefs = tmp_path / "preferences.json"
    if paired:
        (tmp_path / "device_tokens.json").write_text("{}")
    assert _handover(wd, prefs) == 0
    assert _prefs(prefs).get("checkin_every_hours") == expected
    assert all(e.get("job") != "email-flash" for e in _entries(wd))


def test_a_check_in_that_moves_says_so(tmp_path, capsys) -> None:
    """Not anchored to morning: the step survives, the times don't, and the
    deploy output is the only place anyone will see that."""
    wd = _handover_dir(tmp_path, [*_overnight(), _morning("0 8 * * *"), _flash("0 9,12,15,18 * * *")])
    assert _handover(wd, tmp_path / "preferences.json") == 0
    assert "times change from ['0 9,12,15,18 * * *'] to ['0 11,14,17,20 * * *']" in capsys.readouterr().out


def test_a_check_in_that_keeps_its_times_says_nothing_about_them(tmp_path, capsys) -> None:
    wd = _handover_dir(tmp_path, [*_overnight(), _morning("0 8 * * *"), _flash()])
    assert _handover(wd, tmp_path / "preferences.json") == 0
    assert "times change" not in capsys.readouterr().out


def test_junk_in_a_check_in_entry_cannot_abort_the_deploy(tmp_path, capsys) -> None:
    """The removed entries are never validated, and the agent writes them."""
    junk = {"id": "email-flash", "cron": 7, "job": "email-flash"}
    wd = _handover_dir(tmp_path, [*_overnight(), _morning("0 8 * * *"), junk])
    assert _handover(wd, tmp_path / "preferences.json") == 0
    assert "was 7" in capsys.readouterr().out


def test_a_checkin_already_set_is_left_alone(tmp_path) -> None:
    wd = _handover_dir(tmp_path, [*_overnight(), _morning("0 8 * * *"), _flash("0 10,12,14 * * *")])
    prefs = tmp_path / "preferences.json"
    prefs.write_text('{"checkin_every_hours": 5}')
    assert _handover(wd, prefs) == 0
    assert _prefs(prefs)["checkin_every_hours"] == 5


def test_handover_seeds_the_day_from_the_boxs_own_times(tmp_path) -> None:
    """Mike's box: morning 07:30, consolidate 01:00 -> 07:30 to 23:00."""
    wd = _handover_dir(tmp_path, [*_overnight(), _morning("30 7 * * *")])
    prefs = tmp_path / "preferences.json"
    prefs.write_text('{"face": 3}')
    assert _handover(wd, prefs) == 0
    assert _prefs(prefs) == {"face": 3, "day_start": "07:30", "day_end": "23:00"}


def test_a_night_owl_box_keeps_its_late_tidy(tmp_path) -> None:
    wd = _handover_dir(tmp_path, [*_overnight("30 3 * * *", "0 4 * * *"), _morning("0 9 * * *")])
    prefs = tmp_path / "preferences.json"
    assert _handover(wd, prefs) == 0
    assert _prefs(prefs) == {"day_start": "09:00", "day_end": "02:00"}


@pytest.mark.parametrize(
    "consolidate, morning",
    [
        ("0 1 * * 1-5", "0 8 * * *"),  # not every day: no single end to derive
        ("0 1 * * *", "0 7,8 * * *"),
    ],
)
def test_an_odd_schedule_falls_back_to_the_defaults_per_field(tmp_path, consolidate, morning) -> None:
    wd = _handover_dir(tmp_path, [*_overnight(consolidate=consolidate), _morning(morning)])
    prefs = tmp_path / "preferences.json"
    assert _handover(wd, prefs) == 0
    assert not prefs.exists()
    assert [e["id"] for e in _entries(wd)] == ["morning"]


def test_a_day_that_would_not_validate_is_not_written(tmp_path, capsys) -> None:
    # consolidate 06:00 -> end 04:00, outside 20:00-03:00.
    wd = _handover_dir(tmp_path, [*_overnight(consolidate="0 6 * * *"), _morning("0 8 * * *")])
    prefs = tmp_path / "preferences.json"
    assert _handover(wd, prefs) == 0
    assert not prefs.exists()
    assert "day_end_range" in capsys.readouterr().out
    assert [e["id"] for e in _entries(wd)] == ["morning"], "the entries still go"


def test_a_day_already_set_is_left_alone(tmp_path) -> None:
    wd = _handover_dir(tmp_path, [*_overnight(), _morning("30 7 * * *")])
    prefs = tmp_path / "preferences.json"
    prefs.write_text('{"day_start": "06:00", "day_end": "22:00"}')
    assert _handover(wd, prefs) == 0
    assert _prefs(prefs) == {"day_start": "06:00", "day_end": "22:00"}


def test_handover_defers_and_touches_nothing_when_the_rest_is_invalid(tmp_path) -> None:
    wd = _handover_dir(tmp_path, [*_overnight(), {"id": "Bad Id", "cron": "0 9 * * *", "job": "morning"}])
    prefs = tmp_path / "preferences.json"
    before = _snapshot(wd)
    assert _handover(wd, prefs) == 75
    assert _snapshot(wd) == before
    assert not prefs.exists()


def test_a_deferred_handover_still_seeds_the_day(tmp_path) -> None:
    """The rejection tells Marlow to delete these entries; if it does before
    the next deploy, the day would otherwise go with them."""
    entries = [*_overnight(consolidate="0 2 * * *"), _morning("30 7 * * *"),
               {"id": "Bad Id", "cron": "0 9 * * *", "job": "morning"}]
    wd = _handover_dir(tmp_path, entries)
    prefs = tmp_path / "preferences.json"
    assert _handover(wd, prefs) == 75
    assert _prefs(prefs) == {"day_start": "07:30", "day_end": "00:00"}
    assert len(_entries(wd)) == 4, "the store waits for the fix"


def test_handover_defers_on_a_missing_or_unreadable_store(tmp_path) -> None:
    wd = tmp_path / "marlow-identity"
    (wd / "cron").mkdir(parents=True)
    prefs = tmp_path / "preferences.json"
    assert _handover(wd, prefs) == 75
    (wd / "cron" / "schedules.json").write_text("{not json")
    assert _handover(wd, prefs) == 75


def test_handover_second_run_is_a_noop(tmp_path, capsys) -> None:
    wd = _handover_dir(tmp_path, [*_overnight(), _morning("30 7 * * *")])
    prefs = tmp_path / "preferences.json"
    assert _handover(wd, prefs) == 0
    after = (_snapshot(wd), _prefs(prefs))
    capsys.readouterr()
    assert _handover(wd, prefs) == 0
    assert (_snapshot(wd), _prefs(prefs)) == after
    assert "nothing to hand over" in capsys.readouterr().out
