"""The identity/schedules read surface (docs/tasks/ux-lane.md §3a)."""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from server import transparency
from server.config import settings as real_settings
from server.schedules import ScheduleEntry
from server.transparency import next_fire_at, recurrence

TORONTO = ZoneInfo("America/Toronto")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A working dir with SOUL, USER, CLAUDE.md and a memory dir."""
    work = tmp_path / "work"
    (work / "memory").mkdir(parents=True)
    (work / "SOUL.md").write_text("soul")
    (work / "USER.md").write_text("user")
    (work / "CLAUDE.md").write_text("instructions")
    monkeypatch.setattr(
        transparency,
        "settings",
        dataclasses.replace(
            real_settings,
            working_dir=work,
            soul_path=work / "SOUL.md",
            user_path=work / "USER.md",
            memory_dir=work / "memory",
        ),
    )
    return work


def _store(work, entries, tz="America/Toronto"):
    (work / "cron").mkdir(exist_ok=True)
    path = work / "cron" / "schedules.json"
    path.write_text(json.dumps({"timezone": tz, "entries": entries}))
    return path


def _ids(work=None):
    return [f.id for f in transparency.list_files()]


# --- the file index ------------------------------------------------------------

def test_order_kinds_and_description(home):
    (home / "memory" / "b.md").write_text("---\ndescription: about b\n---\nbody")
    (home / "memory" / "a.md").write_text("# a\n\nfirst line of a\n")
    (home / "cron").mkdir()
    (home / "cron" / "morning.md").write_text("do the morning")
    (home / "cron" / "alpha.md").write_text("alpha")
    _store(home, [
        {"id": "m", "cron": "0 8 * * *", "job": "morning"},
        {"id": "m2", "cron": "0 9 * * *", "job": "morning"},  # dedupes
        {"id": "a", "cron": "0 10 * * *", "job": "alpha"},
    ])
    index = transparency.identity_index()
    assert [(f["id"], f["kind"]) for f in index["files"]] == [
        ("SOUL.md", "soul"), ("USER.md", "user"), ("CLAUDE.md", "instructions"),
        ("memory/a.md", "memory"), ("memory/b.md", "memory"),
        ("cron/alpha.md", "job"), ("cron/morning.md", "job"),
    ]
    by_id = {f["id"]: f for f in index["files"]}
    assert by_id["memory/b.md"]["description"] == "about b"
    assert by_id["memory/a.md"]["description"] == "first line of a"
    assert by_id["SOUL.md"]["description"] is None
    assert by_id["SOUL.md"]["size"] == 4
    assert isinstance(by_id["SOUL.md"]["modified_at"], float)


def test_missing_soul_is_omitted_not_an_error(home):
    (home / "SOUL.md").unlink()
    assert _ids() == ["USER.md", "CLAUDE.md"]


def test_missing_memory_dir_is_fine(home):
    (home / "memory").rmdir()
    assert _ids() == ["SOUL.md", "USER.md", "CLAUDE.md"]


def test_outside_working_dir_gets_a_stable_id(home, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "mem").mkdir(parents=True)
    (elsewhere / "SOUL.md").write_text("s")
    (elsewhere / "mem" / "x.md").write_text("x")
    monkeypatch.setattr(
        transparency, "settings",
        dataclasses.replace(
            transparency.settings,
            soul_path=elsewhere / "SOUL.md",
            memory_dir=elsewhere / "mem",
        ),
    )
    assert _ids() == ["SOUL.md", "USER.md", "CLAUDE.md", "memory/x.md"]


def test_memory_lists_only_top_level_md(home):
    (home / "memory" / "keep.md").write_text("k")
    (home / "memory" / "notes.txt").write_text("t")
    (home / "memory" / "sub").mkdir()
    (home / "memory" / "sub" / "x.md").write_text("x")
    (home / "memory" / "dir.md").mkdir()
    assert "memory/keep.md" in _ids()
    assert [i for i in _ids() if i.startswith("memory/")] == ["memory/keep.md"]


def test_symlink_in_memory_is_not_listed_and_404s(home, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("secret stuff")
    (home / "memory" / "link.md").symlink_to(outside)
    assert "memory/link.md" not in _ids()
    assert transparency.read_file("memory/link.md") is None


def test_secret_path_in_memory_is_not_listed(home):
    (home / "memory" / ".env.md").write_text("KEY=1")
    assert "memory/.env.md" not in _ids()
    assert transparency.read_file("memory/.env.md") is None


def test_agent_config_path_is_dropped(home, monkeypatch):
    (home / "memory" / "cfg.md").write_text("c")
    monkeypatch.setattr(
        transparency, "is_agent_config_path", lambda p: p.endswith("cfg.md")
    )
    assert "memory/cfg.md" not in _ids()


@pytest.mark.parametrize("bad", [
    "../SOUL.md", "memory/../SOUL.md", "/etc/passwd", "memory/sub/x.md",
    "memory/nothere.md", "", "cron/schedules.json",
])
def test_read_file_rejects_everything_not_indexed(home, bad):
    (home / "memory" / "sub").mkdir()
    (home / "memory" / "sub" / "x.md").write_text("x")
    (home / "cron").mkdir()
    _store(home, [])
    assert transparency.read_file(bad) is None


def test_read_file_returns_text(home):
    body = transparency.read_file("SOUL.md")
    assert body["text"] == "soul" and body["kind"] == "soul"
    assert body["truncated"] is False and body["id"] == "SOUL.md"


def test_read_file_truncates_at_the_cap(home, monkeypatch):
    monkeypatch.setattr(transparency, "MAX_FILE_BYTES", 10)
    (home / "memory" / "big.md").write_text("x" * 50)
    body = transparency.read_file("memory/big.md")
    assert body["truncated"] is True and body["text"] == "x" * 10
    (home / "memory" / "exact.md").write_text("y" * 10)
    assert transparency.read_file("memory/exact.md")["truncated"] is False


def test_truncation_mid_codepoint_and_bad_bytes_do_not_fail(home, monkeypatch):
    monkeypatch.setattr(transparency, "MAX_FILE_BYTES", 5)
    (home / "memory" / "u.md").write_text("abcdéé")  # cut inside 2nd byte pair
    assert transparency.read_file("memory/u.md")["text"].startswith("abcd")
    (home / "memory" / "bad.md").write_bytes(b"\xff\xfe\xfd")
    assert transparency.read_file("memory/bad.md")["text"]  # replaced, not raised


def test_symlink_swapped_in_after_listing_is_refused(home, tmp_path, monkeypatch):
    """The enumeration can be a moment stale; O_NOFOLLOW is the backstop."""
    outside = tmp_path / "outside.md"
    outside.write_text("secret")
    target = home / "memory" / "m.md"
    target.write_text("ok")
    real_list = transparency.list_files

    def stale():
        files = real_list()
        target.unlink()
        target.symlink_to(outside)
        return files

    monkeypatch.setattr(transparency, "list_files", stale)
    assert transparency.read_file("memory/m.md") is None


# --- schedules block -----------------------------------------------------------

def test_missing_store(home):
    assert transparency.identity_index()["schedules"] == {
        "timezone": None, "rejected": None, "entries": []}


def test_invalid_store_reports_the_reason(home):
    (home / "cron").mkdir()
    (home / "cron" / "schedules.json").write_text('{"timezone": "Nowhere/Land", "entries": []}')
    view = transparency.identity_index()["schedules"]
    assert view["entries"] == [] and view["timezone"] is None
    assert "timezone" in view["rejected"]


def test_valid_store_fields(home):
    (home / "cron").mkdir()
    (home / "cron" / "morning.md").write_text("go")
    _store(home, [
        {"id": "morning", "cron": "0 8 * * *", "job": "morning"},
        {"id": "dentist", "at": "2099-10-20T08:15", "notify": "Dentist"},
    ])
    view = transparency.schedules_view(datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc))
    assert view["timezone"] == "America/Toronto" and view["rejected"] is None
    morning, dentist = view["entries"]
    assert morning == {
        "id": "morning", "cron": "0 8 * * *", "at": None,
        "recurrence": {"type": "daily", "times": ["08:00"]},
        "next_fire_at": datetime(2026, 9, 30, 8, 0, tzinfo=TORONTO).timestamp(),
        "notify": None, "prompt": None, "job": "morning", "job_file": "cron/morning.md",
    }
    assert dentist["recurrence"] == {"type": "once"}
    assert dentist["notify"] == "Dentist" and dentist["job_file"] is None


def test_job_file_null_when_the_file_was_excluded(home, tmp_path):
    (home / "cron").mkdir()
    outside = tmp_path / "o.md"
    outside.write_text("x")
    (home / "cron" / "morning.md").write_text("go")
    _store(home, [{"id": "morning", "cron": "0 8 * * *", "job": "morning"}])
    (home / "cron" / "morning.md").unlink()
    (home / "cron" / "morning.md").symlink_to(outside)
    entry = transparency.schedules_view(datetime.now(timezone.utc))["entries"][0]
    assert entry["job_file"] is None


# --- next_fire_at --------------------------------------------------------------

def _cron(expr):
    return ScheduleEntry(id="x", cron=expr, notify="n")


def _fire(expr, now):
    ts = next_fire_at(_cron(expr), "America/Toronto", now)
    return None if ts is None else datetime.fromtimestamp(ts, TORONTO)


def test_daily_later_today_vs_tomorrow():
    early = datetime(2026, 9, 29, 6, 0, tzinfo=TORONTO)
    late = datetime(2026, 9, 29, 9, 0, tzinfo=TORONTO)
    assert _fire("0 8 * * *", early) == datetime(2026, 9, 29, 8, 0, tzinfo=TORONTO)
    assert _fire("0 8 * * *", late) == datetime(2026, 9, 30, 8, 0, tzinfo=TORONTO)
    # strictly after: exactly-now rolls to tomorrow
    at = datetime(2026, 9, 29, 8, 0, tzinfo=TORONTO)
    assert _fire("0 8 * * *", at) == datetime(2026, 9, 30, 8, 0, tzinfo=TORONTO)


def test_now_in_another_zone_is_read_in_the_stores_zone():
    now = datetime(2026, 9, 29, 11, 0, tzinfo=timezone.utc)  # 07:00 Toronto
    assert _fire("0 8 * * *", now) == datetime(2026, 9, 29, 8, 0, tzinfo=TORONTO)


def test_weekly_monday():
    now = datetime(2026, 9, 29, 12, 0, tzinfo=TORONTO)  # a Tuesday
    assert _fire("3 8 * * 1", now) == datetime(2026, 10, 5, 8, 3, tzinfo=TORONTO)


def test_dom_and_dow_are_ored_when_both_restricted():
    now = datetime(2026, 9, 29, 12, 0, tzinfo=TORONTO)  # Tuesday the 29th
    # the 1st OR a Friday: Thursday Oct 1 comes before Friday Oct 2
    assert _fire("0 8 1 * 5", now) == datetime(2026, 10, 1, 8, 0, tzinfo=TORONTO)
    # the 3rd OR a Friday: Friday Oct 2 beats the 3rd
    assert _fire("0 8 3 * 5", now) == datetime(2026, 10, 2, 8, 0, tzinfo=TORONTO)
    # a `*/2` day-of-month still counts as starred (Vixie): ANDed with Friday
    assert _fire("0 8 */2 * 5", now) == datetime(2026, 10, 9, 8, 0, tzinfo=TORONTO)


def test_dow_seven_is_sunday():
    now = datetime(2026, 9, 29, 12, 0, tzinfo=TORONTO)
    assert _fire("0 8 * * 7", now) == _fire("0 8 * * 0", now)
    assert _fire("0 8 * * 7", now) == datetime(2026, 10, 4, 8, 0, tzinfo=TORONTO)


def test_dst_spring_forward_gap_is_skipped():
    now = datetime(2027, 3, 13, 12, 0, tzinfo=TORONTO)
    assert _fire("30 2 * * *", now) == datetime(2027, 3, 15, 2, 30, tzinfo=TORONTO)


def test_nothing_within_a_year_is_none():
    now = datetime(2026, 9, 29, 12, 0, tzinfo=TORONTO)
    assert _fire("0 8 31 2 *", now) is None


def test_at_in_the_past_is_returned_as_is():
    entry = ScheduleEntry(id="x", at="2020-01-02T03:04", notify="n")
    now = datetime(2026, 9, 29, tzinfo=TORONTO)
    assert next_fire_at(entry, "America/Toronto", now) == datetime(
        2020, 1, 2, 3, 4, tzinfo=TORONTO).timestamp()
    pinned = ScheduleEntry(id="x", at="2020-01-02T03:04-0500", notify="n")
    assert next_fire_at(pinned, "Asia/Tokyo", now) == datetime(
        2020, 1, 2, 3, 4, tzinfo=timezone.utc).timestamp() + 5 * 3600


# --- recurrence ----------------------------------------------------------------
# Structure only: the wording is the app's (§3a), so these pin the facts the
# app groups and phrases from, not any sentence.

@pytest.mark.parametrize("cron,expected", [
    ("0 8 * * *", {"type": "daily", "times": ["08:00"]}),
    ("57 20 * * *", {"type": "daily", "times": ["20:57"]}),
    ("0 8,20 * * *", {"type": "daily", "times": ["08:00", "20:00"]}),
    ("3 8 * * 1", {"type": "weekly", "weekdays": [1], "times": ["08:03"]}),
    # 7 is cron's second Sunday; it must come out as 0, once.
    ("3 8 * * 7", {"type": "weekly", "weekdays": [0], "times": ["08:03"]}),
    ("3 8 * * 0,7", {"type": "weekly", "weekdays": [0], "times": ["08:03"]}),
    ("0 8 * * 1-5", {"type": "weekly", "weekdays": [1, 2, 3, 4, 5], "times": ["08:00"]}),
    ("0 8 * * 6,0", {"type": "weekly", "weekdays": [0, 6], "times": ["08:00"]}),
    ("0 8 1 * *", {"type": "monthly", "days": [1], "times": ["08:00"]}),
    ("0 8 1,15 * *", {"type": "monthly", "days": [1, 15], "times": ["08:00"]}),
    # Every half hour, 9 to 5: daily, with all its times -- the app decides
    # whether seventeen of them are worth listing.
    ("*/30 9-17 * * *", {"type": "daily", "times": [
        f"{h:02d}:{m:02d}" for h in range(9, 18) for m in (0, 30)]}),
    ("0 8 1 6 *", {"type": "other"}),      # month restricted
    ("0 8 1 * 5", {"type": "other"}),      # both day fields: cron ORs them
    ("0 8 */2 * *", {"type": "other"}),    # stepped day-of-month isn't monthly
    ("0 8 * * */2", {"type": "other"}),    # nor a stepped weekday weekly
])
def test_recurrence_cron(cron, expected):
    assert recurrence(_cron(cron)) == expected


def test_recurrence_at_is_once_whatever_its_spelling():
    assert recurrence(ScheduleEntry(id="x", at="2026-10-20T08:15", notify="n")) == {"type": "once"}
    assert recurrence(ScheduleEntry(id="x", at="2026-01-05T20:07-0500", job="j")) == {"type": "once"}


# --- routes --------------------------------------------------------------------

def test_routes_require_a_device_token(home):
    from server.app import public_app

    with TestClient(public_app) as client:
        for url in ("/v1/identity", "/v1/identity/file?id=SOUL.md"):
            assert client.get(url).status_code == 401
            bad = {"Authorization": "Bearer not-a-token"}
            assert client.get(url, headers=bad).status_code == 401


def test_routes_serve_the_index_and_a_file(home, paired_device_token):
    from server.app import public_app

    headers = {"Authorization": f"Bearer {paired_device_token}"}
    with TestClient(public_app) as client:
        index = client.get("/v1/identity", headers=headers).json()
        assert set(index) == {"files", "schedules"}
        assert index["files"][0]["id"] == "SOUL.md"
        got = client.get("/v1/identity/file", params={"id": "SOUL.md"}, headers=headers).json()
        assert got["text"] == "soul" and set(got) == {"id", "kind", "modified_at", "text", "truncated"}


def test_file_route_404s_are_identical(home, paired_device_token, tmp_path):
    from server.app import public_app

    (tmp_path / "outside.md").write_text("x")
    (home / "memory" / "link.md").symlink_to(tmp_path / "outside.md")
    (home / "memory" / ".env.md").write_text("K=1")
    headers = {"Authorization": f"Bearer {paired_device_token}"}
    bodies = set()
    with TestClient(public_app) as client:
        for bad in ("../SOUL.md", "memory/../SOUL.md", str(tmp_path / "outside.md"),
                    "memory/sub/x.md", "nope.md", "memory/link.md", "memory/.env.md"):
            r = client.get("/v1/identity/file", params={"id": bad}, headers=headers)
            assert r.status_code == 404
            bodies.add(r.text)
    assert bodies == {'{"detail":"unknown file"}'}
