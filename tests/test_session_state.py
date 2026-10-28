"""server/session_state.py — the file that keeps nightly consolidation and
session rollover working across a restart.

The behaviour under test is mostly failure behaviour. This is bookkeeping in
service of a cron job that runs once a day: every path here must degrade to
"the old in-memory behaviour" rather than raise, because the alternative is a
phone message that never gets answered over a bookkeeping problem.
"""
from __future__ import annotations

import json

import pytest

from server.session_state import SessionState


@pytest.fixture
def state(tmp_path):
    return SessionState(path=tmp_path / "session_state.json")


def test_both_facts_round_trip_through_the_file(tmp_path):
    path = tmp_path / "session_state.json"
    first = SessionState(path=path)
    first.note_conversation("sess-1")
    first.retire("sess-0")

    reopened = SessionState(path=path)
    assert reopened.conversation_session_id == "sess-1"
    assert reopened.is_retired("sess-0") is True
    assert reopened.is_retired("sess-1") is False


def test_a_missing_file_is_not_an_error(tmp_path):
    """First boot on a fresh box, and every CI run."""
    state = SessionState(path=tmp_path / "nope.json")
    assert state.conversation_session_id is None
    assert state.is_retired("anything") is False


def test_a_corrupt_file_starts_empty_instead_of_raising(tmp_path, caplog):
    """Degrades to exactly the pre-2026-08-16 in-memory behaviour: that
    night's consolidate may skip, and the next completed phone turn repairs
    it. Loud in the log, invisible to Mike."""
    path = tmp_path / "session_state.json"
    path.write_text("{ this is not json")

    state = SessionState(path=path)
    assert state.conversation_session_id is None
    assert "unreadable" in caplog.text

    state.note_conversation("sess-1")
    assert SessionState(path=path).conversation_session_id == "sess-1"


@pytest.mark.parametrize("blob", [
    '{"retired_sessions": 5}',           # not iterable at all
    '{"retired_sessions": true}',
    '{"retired_sessions": "sess-1"}',    # iterable, but per-character
    '[]',                                # valid JSON, wrong shape
    'null',
    '"just a string"',
    '{"last_attended_session_id": ["a"]}',
])
def test_no_file_content_can_stop_the_server_from_starting(tmp_path, blob, caplog):
    """The singleton at the bottom of session_state.py is constructed at
    *import*, so anything escaping `_load` is an ImportError on server.queue —
    the server does not come up at all, and nothing logs why.

    `{"retired_sessions": 5}` did exactly that: `_load` caught OSError,
    JSONDecodeError and AttributeError, and the list comprehension raised
    TypeError (review, 2026-08-16). Parametrized over shapes rather than
    fixing the one known value, since the bug was the enumeration, not the
    type.
    """
    path = tmp_path / "session_state.json"
    path.write_text(blob)

    state = SessionState(path=path)  # must not raise

    assert state.conversation_session_id is None
    assert state.is_retired("sess-1") is False
    # And it must still be usable afterwards, not just non-fatal.
    state.note_conversation("sess-2")
    assert SessionState(path=path).conversation_session_id == "sess-2"


def test_a_hand_edited_file_with_junk_entries_is_tolerated(tmp_path):
    """This is the one piece of state worth poking at over SSH — to force a
    rollover, or to undo one. It should survive a clumsy edit."""
    path = tmp_path / "session_state.json"
    path.write_text(json.dumps({
        "last_attended_session_id": {"not": "a string"},
        "retired_sessions": ["good-1", None, 42, "good-2"],
    }))

    state = SessionState(path=path)
    assert state.conversation_session_id is None
    assert state.is_retired("good-1") and state.is_retired("good-2")


def test_an_unwritable_location_does_not_break_the_turn(tmp_path, caplog):
    """A save failure must never propagate: `_drain` calls this on the happy
    path of every phone turn, and losing a reply over it would be a far worse
    bug than losing a night's rollover."""
    # The parent is a regular FILE, not a 0o500 directory: root ignores mode
    # bits, so under a root-run suite (a cloud container, 2026-10-01) the chmod
    # version wrote happily and this test failed for the wrong reason. A file
    # where a directory should be fails for every uid.
    (tmp_path / "nested").write_text("not a directory")
    unwritable = tmp_path / "nested" / "session_state.json"
    state = SessionState(path=unwritable)
    state.note_conversation("sess-1")  # must not raise
    assert state.conversation_session_id == "sess-1", "in-memory still works"
    assert "could not persist" in caplog.text


def test_retired_list_is_capped(tmp_path):
    """One entry per calendar day, so the cap is years away — but nothing
    else ever removes an id, and the file is read at every startup."""
    from server import session_state as module

    path = tmp_path / "session_state.json"
    state = SessionState(path=path)
    for i in range(module._MAX_RETIRED + 25):
        state.retire(f"sess-{i}")

    reopened = SessionState(path=path)
    assert reopened.is_retired("sess-0") is False, "oldest evicted"
    assert reopened.is_retired(f"sess-{module._MAX_RETIRED + 24}") is True


def test_retire_reports_whether_it_was_new(state):
    """The caller owns the log line, so it must fire once per retirement and
    not once per call."""
    assert state.retire("sess-1") is True
    assert state.retire("sess-1") is False


def test_note_attended_skips_the_write_when_unchanged(tmp_path):
    """Called on every completed phone turn. A back-and-forth exchange stays
    in one session, so without this it is a disk write per message."""
    path = tmp_path / "session_state.json"
    state = SessionState(path=path)
    state.note_conversation("sess-1")
    before = path.stat().st_mtime_ns

    state.note_conversation("sess-1")
    assert path.stat().st_mtime_ns == before


def test_reload_picks_up_an_external_edit(tmp_path):
    """Backs the SSH workflow the docstring advertises, and is what the test
    suite uses to isolate cases."""
    path = tmp_path / "session_state.json"
    state = SessionState(path=path)
    state.retire("sess-1")

    path.write_text(json.dumps({"last_attended_session_id": "sess-9", "retired_sessions": []}))
    state.reload()

    assert state.is_retired("sess-1") is False
    assert state.conversation_session_id == "sess-9"
