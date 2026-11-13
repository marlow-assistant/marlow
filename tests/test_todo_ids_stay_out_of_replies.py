"""To-do ids are for the tools, never for the user (raised 2026-11-10).

The app never shows an id, yet Marlow wrote "t13" in replies and in a morning
brief. Every prompt that names an item put the id first, in backticks, like a
label: so each one now leads with the title, carries the id last as
"id t13", and says once that ids are for the tools only."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from server import todo_reminders, todo_tools, todos, turn_context
from server.todos import ID_RULE, TodoStore, todo_context

ZONE = ZoneInfo("America/Toronto")
T0 = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.at = T0

    def __call__(self) -> datetime:
        return self.at


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path, monkeypatch, clock) -> TodoStore:
    s = TodoStore(path=tmp_path / "todos.json", state_path=tmp_path / "state.json", clock=clock, zone=lambda: ZONE)
    for module in (todos, todo_reminders, todo_tools):
        monkeypatch.setattr(module, "todo_store", s)
    turn_context._reset_gap_for_tests()
    return s


def _title_leads(text: str, title: str, item_id: str) -> None:
    assert f"`{item_id}`" not in text
    assert f"id {item_id}" in text
    assert text.index(title) < text.index(f"id {item_id}")
    assert ID_RULE in text


def test_the_briefing_block(store) -> None:
    store.add("Book the dentist", due="2026-11-06 09:00", source="user")
    store.add("Pay the water bill", source="inferred")
    block = todo_reminders.briefing_block()
    _title_leads(block, "Book the dentist", "t1")
    _title_leads(block, "Pay the water bill", "t2")


def test_the_reminder_turn(store) -> None:
    item = store.add("Book the dentist", due="2026-11-04 11:00", source="user")
    _title_leads(todo_reminders.reminder_prompt([item]), "Book the dentist", "t1")


def test_the_stale_proposal_sentence(store, clock) -> None:
    store.add("Pay the water bill", source="inferred")
    clock.at = T0 + timedelta(days=2)
    _title_leads(todo_reminders.stale_proposals_sentence(), "Pay the water bill", "t1")


def test_the_tapped_item_line(store) -> None:
    item = store.add("Book the dentist", source="user")
    _title_leads(todo_context(item), "Book the dentist", "t1")


def test_the_rule_keeps_the_blocks_closed() -> None:
    # It sits inside bracketed blocks, so it must not end one.
    assert "[" not in ID_RULE and "]" not in ID_RULE
