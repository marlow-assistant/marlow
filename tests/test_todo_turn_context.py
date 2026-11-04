"""The to-do list in a turn's context: the briefings work from the store, and
a proposal unconfirmed for two days is raised once more on a chat turn
(docs/tasks/todos-and-reminders.md, part 2)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from server import todo_reminders, todo_tools, todos, turn_context
from server.app import internal_app
from server.queue import work_queue
from server.todos import TodoStore

ZONE = ZoneInfo("America/Toronto")
T0 = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)  # 10:00 in Toronto


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
    monkeypatch.setattr(todos, "todo_store", s)
    monkeypatch.setattr(todo_reminders, "todo_store", s)
    monkeypatch.setattr(todo_tools, "todo_store", s)
    turn_context._reset_gap_for_tests()
    return s


def test_the_briefing_block_lists_open_and_proposed_and_marks_overdue(store, clock) -> None:
    store.add("Book the dentist", notes="the cleaning", due="2026-11-04 11:00", source="user")
    store.add("Renew the passport", due="2026-11-02", source="user")
    store.add("Call the bank", source="user")
    store.add("Pay the water bill", source="inferred")
    store.add("Old thing", source="user")
    store.done("t5")
    store.add("Never mind", source="user")
    store.drop("t6")
    clock.at = T0 + timedelta(hours=2)
    block = todo_reminders.briefing_block()
    assert block.startswith("[To-do list") and block.endswith("]")
    assert "`t1` \"Book the dentist\" (due Wed 2026-11-04 11:00; overdue). Notes: the cleaning" in block
    assert "`t2` \"Renew the passport\" (due Mon 2026-11-02; overdue)" in block
    assert "`t3` \"Call the bank\"" in block
    assert "`t4` \"Pay the water bill\"" in block
    # Proposals in a part of their own, after the open items.
    assert block.index("`t4`") > block.index("`t3`") and "proposed" in block.lower()[block.index("`t3`"):block.index("`t4`")]
    assert "Old thing" not in block and "Never mind" not in block


def test_a_date_due_today_is_not_overdue(store) -> None:
    store.add("Renew the passport", due="2026-11-04", source="user")
    assert "overdue" not in todo_reminders.briefing_block()


def test_an_empty_list_says_so_so_the_brief_doesnt_go_looking(store) -> None:
    assert "empty" in todo_reminders.briefing_block()


def test_the_briefing_block_keeps_list_text_inside_it(store) -> None:
    store.add("Pay ] the [bill", notes="one\ntwo ]", source="user")
    block = todo_reminders.briefing_block()
    assert block.count("[") == 1 and block.count("]") == 1
    assert not any(line.startswith("Pay") or line.startswith("two") for line in block.splitlines())


@pytest.mark.parametrize("job,listed", [("morning", True), ("week-ahead", True), ("ideas", False)])
def test_the_briefing_jobs_get_the_list_in_front_of_their_prompt(store, monkeypatch, job, listed) -> None:
    store.add("Call the bank", source="user")
    prompts = []
    monkeypatch.setattr(work_queue, "enqueue", lambda prompt, **kw: prompts.append(prompt) or "turn-1")
    with TestClient(internal_app) as client:
        assert client.post("/internal/cron/message", json={"job": job, "text": "Good morning."}).status_code == 202
    assert prompts[0].endswith("Good morning.")
    assert ("`t1` \"Call the bank\"" in prompts[0]) is listed


def test_a_user_entry_named_morning_does_not_get_the_list(store, monkeypatch) -> None:
    """An entry id is model-chosen; only the shipped job is a briefing."""
    store.add("Call the bank", source="user")
    prompts = []
    monkeypatch.setattr(work_queue, "enqueue", lambda prompt, **kw: prompts.append(prompt) or "turn-1")
    with TestClient(internal_app) as client:
        client.post("/internal/cron/message", json={"entry": "morning", "text": "hi"})
    assert prompts == ["hi"]


def test_a_proposal_two_days_old_is_raised_once_on_a_chat_turn(store, clock) -> None:
    store.add("Pay the water bill", source="inferred")
    clock.at = T0 + timedelta(days=1)
    assert "Pay the water bill" not in turn_context.build_turn_context(resume=False, source="ios")
    clock.at = T0 + timedelta(days=2)
    # Not on an unattended turn: nobody is there to answer the question.
    assert "Pay the water bill" not in turn_context.build_turn_context(resume=False, source="cron")
    first = turn_context.build_turn_context(resume=False, source="ios")
    assert "`t1` \"Pay the water bill\"" in first and "todo_accept" in first
    assert "Pay the water bill" not in turn_context.build_turn_context(resume=False, source="ios")


def test_an_accepted_proposal_is_not_raised(store, clock) -> None:
    store.add("Pay the water bill", source="inferred")
    store.accept("t1")
    clock.at = T0 + timedelta(days=3)
    assert "Pay the water bill" not in turn_context.build_turn_context(resume=False, source="ios")


def test_the_briefing_clips_long_notes(store) -> None:
    store.add("Call the bank", notes="x" * todos.MAX_NOTES_CHARS, source="user")
    block = todo_reminders.briefing_block()
    assert len(block) < todo_reminders.BRIEFING_NOTES_CHARS + 400
    assert "…" in block
