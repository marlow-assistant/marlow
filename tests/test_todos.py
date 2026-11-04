"""The to-do store: its one writer, the item states, and the reminder
bookkeeping the server keeps beside it (docs/tasks/todos-and-reminders.md,
part 2)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from server import todos
from server.todos import TodoError, TodoStore

ZONE = ZoneInfo("America/Toronto")
# A Wednesday, 10:00 in Toronto.
T0 = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, at: datetime = T0) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **kw) -> None:
        self.at = self.at + timedelta(**kw)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path, clock) -> TodoStore:
    return TodoStore(
        path=tmp_path / "todos" / "todos.json",
        state_path=tmp_path / "data" / "todo_reminders.json",
        clock=clock,
        zone=lambda: ZONE,
    )


def _on_disk(store: TodoStore) -> dict:
    return json.loads(store.path.read_text())


# --- adding --------------------------------------------------------------------


def test_the_users_own_words_make_an_open_item(store) -> None:
    item = store.add("Book the dentist", source="user")
    assert item["state"] == "open"
    assert item["id"] == "t1"
    assert _on_disk(store)["items"][0]["title"] == "Book the dentist"


def test_an_inferred_item_is_proposed(store) -> None:
    assert store.add("Pay the water bill", source="inferred")["state"] == "proposed"


def test_ids_come_from_the_store_and_are_never_reused(store) -> None:
    a = store.add("one", source="user")
    store.drop(a["id"])
    b = store.add("two", source="user")
    assert (a["id"], b["id"]) == ("t1", "t2")
    # Survives a fresh instance on the same files.
    again = TodoStore(path=store.path, state_path=store.state_path, clock=store._clock, zone=lambda: ZONE)
    assert again.add("three", source="user")["id"] == "t3"


def test_source_must_be_user_or_inferred(store) -> None:
    with pytest.raises(TodoError, match="source"):
        store.add("x", source="marlow")


@pytest.mark.parametrize("title", ["", "   ", "x" * 201])
def test_a_title_must_be_short_and_not_blank(store, title) -> None:
    with pytest.raises(TodoError, match="title"):
        store.add(title, source="user")


def test_control_characters_are_flattened(store) -> None:
    item = store.add("Call\nthe\x00 plumber", notes="line one\nline two", source="user")
    assert item["title"] == "Call the plumber"
    # Notes keep their newlines: they are prose, not a one-line label.
    assert item["notes"] == "line one\nline two"


def test_notes_are_capped(store) -> None:
    with pytest.raises(TodoError, match="notes"):
        store.add("x", notes="n" * (todos.MAX_NOTES_CHARS + 1), source="user")


def test_an_active_duplicate_is_refused_with_its_id(store) -> None:
    store.add("Book the dentist", source="user")
    with pytest.raises(TodoError, match="t1"):
        store.add("  book THE dentist ", source="inferred")


def test_a_dropped_item_is_not_proposed_again(store) -> None:
    store.add("Renew the magazine", source="inferred")
    store.drop("t1")
    with pytest.raises(TodoError, match="dropped"):
        store.add("Renew the magazine", source="inferred")
    # The user asking for it again is their call.
    assert store.add("Renew the magazine", source="user")["state"] == "open"


def test_the_active_list_is_capped(store, monkeypatch) -> None:
    monkeypatch.setattr(todos, "MAX_ACTIVE", 3)
    for n in range(3):
        store.add(f"item {n}", source="user")
    with pytest.raises(TodoError, match="full"):
        store.add("one more", source="user")


# --- due -----------------------------------------------------------------------


def test_a_date_is_kept_as_a_date(store) -> None:
    assert store.add("x", due="2026-11-06", source="user")["due"] == "2026-11-06"


def test_a_local_time_is_pinned_to_the_users_zone(store) -> None:
    item = store.add("x", due="2026-11-06 09:00", source="user")
    # EST after the 2026-11-01 change: -05:00.
    assert item["due"] == "2026-11-06T09:00:00-05:00"


def test_a_time_with_an_offset_is_kept(store) -> None:
    item = store.add("x", due="2026-11-06T09:00:00+01:00", source="user")
    assert item["due"] == "2026-11-06T09:00:00+01:00"


@pytest.mark.parametrize("due", ["Friday", "2026-13-01", "2026-11-06 25:00", "tomorrow 9am"])
def test_an_unreadable_due_is_refused_with_the_format(store, due) -> None:
    with pytest.raises(TodoError, match="YYYY-MM-DD"):
        store.add("x", due=due, source="user")


def test_a_time_already_past_is_refused(store) -> None:
    with pytest.raises(TodoError, match="passed"):
        store.add("x", due="2026-11-04 09:00", source="user")


def test_a_past_date_is_allowed_as_overdue(store) -> None:
    assert store.add("x", due="2026-11-01", source="user")["due"] == "2026-11-01"


# --- changing state ------------------------------------------------------------


def test_done_closes_and_is_idempotent(store, clock) -> None:
    store.add("x", source="user")
    assert store.done("t1")["state"] == "done"
    assert store.done("t1")["state"] == "done"
    assert store.get("t1")["closed"] == T0.isoformat(timespec="seconds")


def test_a_dropped_item_cannot_then_be_done(store) -> None:
    store.add("x", source="user")
    store.drop("t1")
    with pytest.raises(TodoError, match="dropped"):
        store.done("t1")


def test_accept_moves_a_proposal_to_open(store) -> None:
    store.add("x", source="inferred")
    assert store.accept("t1")["state"] == "open"
    with pytest.raises(TodoError, match="not a proposal"):
        store.accept("t1")


def test_reopen_undoes_done_and_is_idempotent(store, clock) -> None:
    # The pane's untick (Mike, 2026-11-04: a tick he couldn't take back).
    store.add("x", source="user")
    store.done("t1")
    item = store.reopen("t1")
    assert (item["state"], item["closed"]) == ("open", None)
    assert store.reopen("t1")["state"] == "open"


def test_only_a_done_item_can_be_reopened(store) -> None:
    store.add("x", source="user")
    store.drop("t1")
    with pytest.raises(TodoError, match="dropped"):
        store.reopen("t1")
    store.add("y", source="inferred")
    with pytest.raises(TodoError, match="proposed"):
        store.reopen("t2")


def test_a_reopened_title_counts_as_active_again(store) -> None:
    store.add("Book the dentist", source="user")
    store.done("t1")
    store.add("Book the dentist", source="user")
    with pytest.raises(TodoError, match="already"):
        store.reopen("t1")


def test_an_unknown_id_is_named_in_the_error(store) -> None:
    with pytest.raises(TodoError, match="t9"):
        store.done("t9")


def test_update_changes_only_what_is_given(store) -> None:
    store.add("x", notes="why", due="2026-11-06", source="user")
    item = store.update("t1", title="y")
    assert (item["title"], item["notes"], item["due"]) == ("y", "why", "2026-11-06")
    assert store.update("t1", due=None)["due"] is None


def test_a_closed_item_cannot_be_updated(store) -> None:
    store.add("x", source="user")
    store.done("t1")
    with pytest.raises(TodoError, match="done"):
        store.update("t1", title="y")


def test_closed_items_age_out(store, clock) -> None:
    store.add("done one", source="user")
    store.add("dropped one", source="user")
    store.done("t1")
    store.drop("t2")
    clock.advance(days=todos.KEEP_DONE_DAYS + 1)
    store.add("trigger a write", source="user")
    ids = [i["id"] for i in store.items()]
    # Dropped is kept longer: it is what stops a re-proposal.
    assert ids == ["t2", "t3"]


def test_a_corrupt_file_refuses_writes_rather_than_starting_over(store) -> None:
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{not json")
    with pytest.raises(TodoError, match="unreadable"):
        store.add("x", source="user")
    # Nothing replaced what was there: the list is still recoverable by hand.
    assert store.path.read_text() == "{not json"
    assert store.items() == []


# --- snooze --------------------------------------------------------------------


def test_snooze_needs_an_open_item_and_a_future_time(store) -> None:
    store.add("x", source="inferred")
    with pytest.raises(TodoError, match="open"):
        store.snooze("t1", "2026-11-05 09:00")
    store.accept("t1")
    with pytest.raises(TodoError, match="passed"):
        store.snooze("t1", "2026-11-04 09:00")
    item = store.snooze("t1", "2026-11-05 09:00")
    assert item["snoozed_until"] == "2026-11-05T09:00:00-05:00"


def test_snooze_is_bounded(store) -> None:
    store.add("x", source="user")
    with pytest.raises(TodoError, match="year"):
        store.snooze("t1", "2028-01-01 09:00")


# --- reminders -----------------------------------------------------------------


def _due_ids(store, renudge=True) -> list[str]:
    return [i["id"] for i in store.due_for_reminder(renudge=renudge)]


def test_only_an_open_item_with_a_time_fires_at_its_time(store, clock) -> None:
    store.add("timed", due="2026-11-04 11:00", source="user")
    store.add("dated", due="2026-11-04", source="user")
    store.add("undated", source="user")
    store.add("proposed", due="2026-11-04 11:00", source="inferred")
    assert _due_ids(store) == []
    clock.advance(hours=1)
    assert _due_ids(store) == ["t1"]


def test_a_fired_item_waits_for_its_turn_then_renudges_daily(store, clock) -> None:
    store.add("timed", due="2026-11-04 11:00", source="user")
    clock.advance(hours=1)
    fired = store.mark_fired(["t1"])
    assert _due_ids(store) == []  # in flight
    store.settle(fired, delivered=True)
    assert _due_ids(store) == []
    clock.advance(hours=23, minutes=59)
    assert _due_ids(store) == []
    clock.advance(minutes=1)
    assert _due_ids(store) == ["t1"]
    assert _due_ids(store, renudge=False) == []


def test_done_dropped_or_snoozed_stops_the_renudge(store, clock) -> None:
    for title in ("a", "b", "c"):
        store.add(title, due="2026-11-04 11:00", source="user")
    clock.advance(hours=1)
    store.settle(store.mark_fired(["t1", "t2", "t3"]), delivered=True)
    store.done("t1")
    store.drop("t2")
    store.snooze("t3", "2026-11-07 09:00")
    clock.advance(days=1)
    assert _due_ids(store) == []
    clock.at = datetime(2026, 11, 7, 14, 0, tzinfo=timezone.utc)
    assert _due_ids(store) == ["t3"]


def test_a_snooze_makes_an_undated_item_fire(store, clock) -> None:
    store.add("undated", source="user")
    store.snooze("t1", "2026-11-04 12:00")
    clock.advance(hours=2)
    assert _due_ids(store) == ["t1"]


def test_an_undelivered_reminder_falls_back_unless_the_turn_handled_it(store, clock) -> None:
    store.add("a", due="2026-11-04 11:00", source="user")
    store.add("b", due="2026-11-04 11:00", source="user")
    store.add("c", due="2026-11-04 11:00", source="user")
    clock.advance(hours=1)
    fired = store.mark_fired(["t1", "t2", "t3"])
    # During the turn: Marlow marks one done and snoozes another.
    store.done("t1")
    store.snooze("t2", "2026-11-05 09:00")
    fallback = store.settle(fired, delivered=False)
    assert [i["id"] for i in fallback] == ["t3"]


def test_moving_the_due_time_during_the_turn_counts_as_handled(store, clock) -> None:
    store.add("a", due="2026-11-04 11:00", source="user")
    clock.advance(hours=1)
    fired = store.mark_fired(["t1"])
    store.update("t1", due="2026-11-05 11:00")
    assert store.settle(fired, delivered=False) == []
    # And the new time fires as a first reminder, not a renudge.
    clock.at = datetime(2026, 11, 5, 16, 0, tzinfo=timezone.utc)
    assert _due_ids(store) == ["t1"]


def test_a_reminder_in_flight_at_restart_fires_again(store, clock, tmp_path) -> None:
    store.add("a", due="2026-11-04 11:00", source="user")
    clock.advance(hours=1)
    store.mark_fired(["t1"])
    restarted = TodoStore(path=store.path, state_path=store.state_path, clock=clock, zone=lambda: ZONE)
    restarted.recover()
    assert [i["id"] for i in restarted.due_for_reminder(renudge=True)] == ["t1"]


def test_reminder_state_lives_outside_the_item_file(store, clock) -> None:
    store.add("a", due="2026-11-04 11:00", source="user")
    clock.advance(hours=1)
    store.mark_fired(["t1"])
    assert "fired_at" not in store.path.read_text()
    assert "t1" in json.loads(store.state_path.read_text())["items"]


# --- proposals -----------------------------------------------------------------


def test_a_proposal_is_raised_again_once_after_two_days(store, clock) -> None:
    store.add("p", source="inferred")
    clock.advance(days=2, minutes=-1)
    assert store.take_stale_proposals() == []
    clock.advance(minutes=1)
    assert [i["id"] for i in store.take_stale_proposals()] == ["t1"]
    # Once only.
    clock.advance(days=5)
    assert store.take_stale_proposals() == []
    assert store.get("t1")["state"] == "proposed"


# --- what the phone sees -------------------------------------------------------


def test_the_client_list_is_active_items_plus_a_week_of_done(store, clock) -> None:
    store.add("open", source="user")
    store.add("proposed", source="inferred")
    store.add("old done", source="user")
    store.add("dropped", source="user")
    store.done("t3")
    store.drop("t4")
    clock.advance(days=todos.RECENT_DONE_DAYS + 1)
    store.add("new done", source="user")
    store.done("t5")
    assert [i["id"] for i in store.client_items()] == ["t1", "t2", "t5"]


def test_a_snooze_before_the_due_time_still_leaves_the_due_reminder(store, clock) -> None:
    """Snoozing a not-yet-due item ("in 2 hours" today, due tomorrow 09:00)
    must not swallow tomorrow's reminder, re-nudge on or off."""
    store.add("Book the dentist", due="2026-11-05 09:00", source="user")
    store.snooze("t1", "2026-11-04 12:00")
    clock.advance(hours=2)
    assert _due_ids(store) == ["t1"]
    store.settle(store.mark_fired(["t1"]), delivered=True)
    clock.at = datetime(2026, 11, 5, 13, 59, tzinfo=timezone.utc)  # 08:59 local
    assert _due_ids(store, renudge=False) == []
    clock.advance(minutes=1)
    assert _due_ids(store, renudge=False) == ["t1"]


def test_a_reminder_recovered_after_a_restart_is_not_shown_as_a_snooze(store, clock) -> None:
    store.add("timed", due="2026-11-04 11:00", source="user")
    clock.advance(hours=1)
    store.mark_fired(["t1"])
    store.recover()
    assert store.get("t1")["snoozed_until"] is None
    assert _due_ids(store) == ["t1"]
