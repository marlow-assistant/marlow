"""server/preferences.py and /v1/preferences: the face and Thinking level
every client shares.

What is worth guarding: nothing is set until a client sets it, a choice
survives a restart, a junk value never lands (or is read back as a face), and
an unpaired caller can neither read nor write it.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from server.app import internal_app, public_app
from server.preferences import EFFORTS, MAX_FACE_ID, DayError, PreferencesStore, preferences_store, validate_day

from tests.test_app import _paired_headers, isolated_pairing_state  # noqa: F401 (fixture)


# What GET answers on a box where nobody has set "Your day".
DEFAULT_DAY = {"day_start": "08:00", "day_end": "23:00", "checkin_every_hours": 3}


@pytest.fixture
def clients(isolated_pairing_state):  # noqa: F811
    with TestClient(public_app) as client, TestClient(internal_app) as internal:
        headers, _ = _paired_headers(client, internal, name="prefs-device")
        yield client, headers


def test_no_face_until_one_is_picked(clients):
    client, headers = clients
    assert client.get("/v1/preferences", headers=headers).json() == DEFAULT_DAY | {"face": None, "effort": None}


def test_a_pick_is_what_every_client_reads(clients):
    client, headers = clients
    assert client.post("/v1/preferences", json={"face": 7}, headers=headers).json() == DEFAULT_DAY | {"face": 7, "effort": None}
    assert client.get("/v1/preferences", headers=headers).json() == DEFAULT_DAY | {"face": 7, "effort": None}
    client.post("/v1/preferences", json={"face": 3, "effort": "high"}, headers=headers)
    assert client.get("/v1/preferences", headers=headers).json() == DEFAULT_DAY | {"face": 3, "effort": "high"}


def test_sending_one_keeps_the_other(clients):
    client, headers = clients
    client.post("/v1/preferences", json={"face": 5, "effort": "low"}, headers=headers)
    client.post("/v1/preferences", json={"effort": "medium"}, headers=headers)
    assert client.get("/v1/preferences", headers=headers).json() == DEFAULT_DAY | {"face": 5, "effort": "medium"}
    client.post("/v1/preferences", json={"face": 8}, headers=headers)
    assert client.get("/v1/preferences", headers=headers).json() == DEFAULT_DAY | {"face": 8, "effort": "medium"}


@pytest.mark.parametrize(
    "body",
    [
        {"face": 0}, {"face": MAX_FACE_ID + 1}, {"face": "7"}, {"face": 7.5}, {"face": True},
        {"effort": "max"}, {"effort": "High"}, {"effort": 2}, {"face": 2, "effort": "xhigh"},
        {}, {"face": None, "effort": None},
        {"day_start": "8:00"}, {"day_start": "08:00:00"}, {"day_end": "24:00"},
        {"day_start": "03:59"}, {"day_end": "19:59"}, {"day_end": "03:01"},
        {"face": 2, "day_end": "04:30"},
        {"checkin_every_hours": 7}, {"checkin_every_hours": -1}, {"checkin_every_hours": "3"},
        {"checkin_every_hours": True}, {"checkin_every_hours": 2.5},
    ],
)
def test_junk_is_refused_and_leaves_the_choice_alone(clients, body):
    client, headers = clients
    client.post("/v1/preferences", json={"face": 4, "effort": "low"}, headers=headers)
    assert client.post("/v1/preferences", json=body, headers=headers).status_code == 422
    assert client.get("/v1/preferences", headers=headers).json() == DEFAULT_DAY | {"face": 4, "effort": "low"}


def test_unpaired_callers_get_nothing(clients):
    client, _ = clients
    assert client.get("/v1/preferences").status_code == 401
    assert client.post("/v1/preferences", json={"face": 2}).status_code == 401
    assert preferences_store.face() is None and preferences_store.effort() is None


def test_survives_a_restart(tmp_path):
    path = tmp_path / "preferences.json"
    PreferencesStore(path).update(face=9, effort="high")
    assert PreferencesStore(path).face() == 9
    assert PreferencesStore(path).effort() == "high"


@pytest.mark.parametrize(
    "text", ["not json", "[1, 2]", '{"face": "9", "effort": "max"}', '{"face": true, "effort": 1}', '{"face": 0}']
)
def test_an_unreadable_or_odd_file_reads_as_nothing_picked(tmp_path, text):
    path = tmp_path / "preferences.json"
    path.write_text(text)
    assert PreferencesStore(path).face() is None
    assert PreferencesStore(path).effort() is None


def test_the_levels_match_the_servers_effort_allowlist():
    """A level the clients offer that the turn path refuses would sync and
    then do nothing."""
    from server.app import _ALLOWED_CLIENT_EFFORTS

    assert set(EFFORTS) == set(_ALLOWED_CLIENT_EFFORTS)


def test_the_file_is_private(tmp_path):
    path = tmp_path / "preferences.json"
    PreferencesStore(path).update(face=2)
    assert path.stat().st_mode & 0o777 == 0o600


# --- "Your day" (docs/tasks/default-infrastructure.md) -------------------------


@pytest.mark.parametrize(
    "start, end",
    [
        ("08:00", "23:00"),  # the defaults
        ("04:00", "20:00"),
        ("12:00", "20:00"),
        ("08:00", "02:00"),  # a night owl: ends after midnight, 6h night
        ("07:00", "03:00"),  # the latest end, exactly a 4h night
        ("06:30", "23:59"),
    ],
)
def test_days_the_harness_can_time(start, end):
    validate_day(start, end)


@pytest.mark.parametrize(
    "start, end, code",
    [
        ("03:59", "23:00", "day_start_range"),
        ("12:01", "23:00", "day_start_range"),
        ("08:00", "19:59", "day_end_range"),
        ("08:00", "03:01", "day_end_range"),
        ("08:00", "12:00", "day_end_range"),
        ("06:00", "03:00", "night_too_short"),  # 3h: no room for both tidy-ups
        ("04:00", "01:00", "night_too_short"),
        ("8:00", "23:00", "bad_time"),
        ("08:00", "23:60", "bad_time"),
    ],
)
def test_days_it_cannot(start, end, code):
    with pytest.raises(DayError) as caught:
        validate_day(start, end)
    assert caught.value.code == code


def test_a_new_end_is_judged_against_the_stored_start(tmp_path):
    store = PreferencesStore(tmp_path / "preferences.json")
    store.update(day_start="07:00")
    store.update(day_end="03:00")  # exactly the 4h night against 07:00
    assert store.day() == ("07:00", "03:00")
    with pytest.raises(DayError):
        store.update(day_start="06:59")
    assert store.day() == ("07:00", "03:00")


def test_only_the_field_sent_is_stored(tmp_path):
    """An unset end keeps meaning "the default", so a later default reaches it."""
    path = tmp_path / "preferences.json"
    store = PreferencesStore(path)
    assert not store.day_is_set()
    store.update(day_start="07:00")
    assert json.loads(path.read_text()) == {"day_start": "07:00"}
    assert store.day_is_set()


def test_an_invalid_stored_day_reads_as_the_defaults(tmp_path):
    path = tmp_path / "preferences.json"
    path.write_text('{"day_start": "13:00", "day_end": "23:00", "face": 3}')
    store = PreferencesStore(path)
    assert store.day() == ("08:00", "23:00")
    assert store.face() == 3


def test_setting_the_day_keeps_face_and_effort(tmp_path):
    store = PreferencesStore(tmp_path / "preferences.json")
    store.update(face=4, effort="low")
    store.update(day_end="01:30")
    assert (store.face(), store.effort(), store.day()) == (4, "low", ("08:00", "01:30"))


def test_the_endpoint_sets_the_day_and_words_nothing(clients, monkeypatch):
    client, headers = clients
    synced = []
    monkeypatch.setattr("server.app.schedules.sync_after_turn", lambda *a: synced.append(1))

    got = client.post("/v1/preferences", json={"day_end": "01:00"}, headers=headers)
    assert got.json() == {"face": None, "effort": None, "day_start": "08:00", "day_end": "01:00", "checkin_every_hours": 3}
    assert synced == [1], "a changed day re-renders the crontab at once"

    client.post("/v1/preferences", json={"face": 2}, headers=headers)
    assert synced == [1], "nothing to re-render when the day didn't move"

    refused = client.post("/v1/preferences", json={"day_start": "04:30"}, headers=headers)
    assert (refused.status_code, refused.json()["detail"]) == (422, "night_too_short")


# --- the check-in interval (tier 2) --------------------------------------------


def test_checkins_default_to_three_hours_and_zero_is_off(tmp_path):
    store = PreferencesStore(tmp_path / "preferences.json")
    assert store.checkin_every_hours() == 3 and not store.checkin_is_set()
    store.update(checkin_every_hours=0)
    assert store.checkin_every_hours() == 0 and store.checkin_is_set()


@pytest.mark.parametrize(
    "text", ['{"checkin_every_hours": 9}', '{"checkin_every_hours": true}', '{"checkin_every_hours": "2"}']
)
def test_an_odd_stored_interval_reads_as_the_default(tmp_path, text):
    path = tmp_path / "preferences.json"
    path.write_text(text)
    assert PreferencesStore(path).checkin_every_hours() == 3


def test_a_changed_interval_re_renders_the_crontab(clients, monkeypatch):
    client, headers = clients
    synced = []
    monkeypatch.setattr("server.app.schedules.sync_after_turn", lambda *a: synced.append(1))
    got = client.post("/v1/preferences", json={"checkin_every_hours": 0}, headers=headers)
    assert got.json()["checkin_every_hours"] == 0
    assert synced == [1]
    client.post("/v1/preferences", json={"checkin_every_hours": 0}, headers=headers)
    assert synced == [1], "unchanged: nothing to re-render"
