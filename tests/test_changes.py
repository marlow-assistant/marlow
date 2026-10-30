"""What's new (docs/tasks/whats-new.md): the real changes.json must parse, and
the box serves only its own half of it."""
from __future__ import annotations

import dataclasses
import json

import pytest
from fastapi.testclient import TestClient

from server import app as app_module
from server import changes
from server.app import public_app


def _entry(**over):
    entry = {"id": 1, "date": "2026-10-23", "part": "server", "category": "feature", "text": "x"}
    entry.update(over)
    return entry


def _real_file():
    return json.loads(changes.CHANGES_PATH.read_text(encoding="utf-8"))


def test_the_real_file_parses():
    # Strict on purpose: this is the check that keeps a typo off the box,
    # where load() would swallow it and the user would just get no card.
    entries = changes.parse(_real_file())
    assert entries, "changes.json has no entries"


def test_the_real_file_is_in_id_order():
    # Append-only by id. Clients key "seen" on these ids, so a reshuffle (a
    # merge that picked the wrong side, usually) is worth stopping on.
    ids = [e["id"] for e in _real_file()["entries"]]
    assert ids == sorted(ids)


@pytest.mark.parametrize(
    "bad",
    [
        _entry(id=0),
        _entry(id=True),
        _entry(id="1"),
        _entry(date="23 Oct"),
        _entry(part="ios"),
        _entry(category="misc"),
        _entry(text="   "),
        {k: v for k, v in _entry().items() if k != "text"},
        {**_entry(), "extra": 1},
    ],
)
def test_parse_refuses_a_malformed_entry(bad):
    with pytest.raises(changes.ChangesError):
        changes.parse({"entries": [bad]})


def test_parse_refuses_duplicate_ids():
    with pytest.raises(changes.ChangesError):
        changes.parse({"entries": [_entry(), _entry(text="y")]})


def test_parse_sorts_newest_first():
    entries = changes.parse({"entries": [_entry(id=1), _entry(id=3), _entry(id=2)]})
    assert [e["id"] for e in entries] == [3, 2, 1]


def test_load_never_raises(tmp_path):
    path = tmp_path / "changes.json"
    path.write_text("{not json", encoding="utf-8")
    assert changes.load(path) == []
    assert changes.load(tmp_path / "missing.json") == []


def test_the_endpoint_requires_a_device_token():
    with TestClient(public_app) as client:
        assert client.get("/v1/changes").status_code == 401


def test_the_endpoint_serves_only_server_entries(monkeypatch, paired_device_token):
    parsed = changes.parse({"entries": [_entry(id=1), _entry(id=2, part="app"), _entry(id=3)]})
    monkeypatch.setattr(changes, "_ENTRIES", parsed)
    headers = {"Authorization": f"Bearer {paired_device_token}"}
    with TestClient(public_app) as client:
        body = client.get("/v1/changes", headers=headers).json()
    assert [e["id"] for e in body["changes"]] == [3, 1]


def test_the_endpoint_reports_onboarding_from_the_record(monkeypatch, tmp_path, paired_device_token):
    """The server's onboarding record, not a search of USER.md: an onboarding
    the cap ended with USER.md unwritten still releases the card, and a box
    that onboarded before the record existed counts as finished."""
    from server import onboarding

    user = tmp_path / "USER.md"
    user.write_text("## Everything else\nNot yet known.\n")
    record = tmp_path / "onboarding.json"
    monkeypatch.setattr(
        "server.app.settings",
        dataclasses.replace(app_module.settings, user_path=user, data_dir=tmp_path),
    )
    headers = {"Authorization": f"Bearer {paired_device_token}"}
    with TestClient(public_app) as client:
        assert client.get("/v1/changes", headers=headers).json()["onboarding_complete"] is False
        for step in onboarding.STEPS:
            onboarding.after_turn(record, user, [(step.id, "skipped")], ())
        assert "Not yet known" in user.read_text()
        assert client.get("/v1/changes", headers=headers).json()["onboarding_complete"] is True

    record.unlink()
    user.write_text("## Everything else\nWorks in marine biology.\n")
    with TestClient(public_app) as client:
        assert client.get("/v1/changes", headers=headers).json()["onboarding_complete"] is True
