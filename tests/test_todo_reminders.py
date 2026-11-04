"""Reminders derived from to-dos, delivered through a Marlow turn, with the
server's plain-title fallback and no silent skip
(docs/tasks/todos-and-reminders.md, part 2)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from server import todo_reminders, todos
from server.invoke import InvocationResult
from server.preferences import preferences_store
from server.push import PushType
from server.queue import TurnStatus, WorkQueue
from server.todos import TodoStore

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
    monkeypatch.setattr(todos, "todo_store", s)
    monkeypatch.setattr(todo_reminders, "todo_store", s)
    preferences_store._reset_for_tests()
    yield s
    preferences_store._reset_for_tests()


@pytest.fixture
def pushes(monkeypatch) -> list:
    sent: list = []
    monkeypatch.setattr("server.queue.push_broadcast_nowait", sent.append)
    return sent


def _reply(text: str | None, *, is_error: bool = False) -> InvocationResult:
    return InvocationResult(
        exit_code=1 if is_error else 0, is_error=is_error, subtype="error" if is_error else "success",
        result_text=text, session_id="s1", permission_denials=[],
    )


async def _settled(wq: WorkQueue, turn_id: str, store: TodoStore) -> None:
    for _ in range(250):
        turn = wq.get_turn(turn_id)
        pending = any(v.get("pending") for v in store._read_state().values())
        if turn.status in (TurnStatus.DONE, TurnStatus.ERROR) and not pending:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("reminder turn never settled")


def _run(store, clock, monkeypatch, reply, *, during=None):
    """Due items -> one tick -> the turn runs with `reply` -> settled.
    Returns (prompts, the system rows posted, the turn)."""
    prompts: list[tuple[str, dict]] = []

    async def fake_run(prompt, **kwargs):
        prompts.append((prompt, kwargs))
        if during:
            during()
        return reply

    monkeypatch.setattr("server.queue.run_claude", fake_run)
    posted: list[str] = []

    async def body():
        wq = WorkQueue()
        real_post = wq.post_system_message

        def post(text, **kw):
            posted.append(text)
            return real_post(text, **kw)

        wq.post_system_message = post
        wq.start()
        try:
            turn_id = await todo_reminders.tick(wq)
            if turn_id is None:
                return None
            await _settled(wq, turn_id, store)
            return wq.get_turn(turn_id)
        finally:
            await wq.stop()

    turn = asyncio.run(body())
    return prompts, posted, turn


def _due_two(store, clock) -> None:
    store.add("Book the dentist", notes="the cleaning", due="2026-11-04 11:00", source="user")
    store.add("Call the bank", due="2026-11-04 11:00", source="user")
    clock.at = T0 + timedelta(hours=1)


def test_nothing_due_starts_no_turn(store, clock, monkeypatch, pushes) -> None:
    store.add("Later", due="2026-11-05 11:00", source="user")
    prompts, posted, turn = _run(store, clock, monkeypatch, _reply("hi"))
    assert turn is None and prompts == [] and posted == []


def test_items_due_together_share_one_unattended_turn_in_the_conversation(store, clock, monkeypatch, pushes) -> None:
    _due_two(store, clock)
    prompts, posted, turn = _run(store, clock, monkeypatch, _reply("Dentist and the bank are due."))
    assert len(prompts) == 1
    prompt, kwargs = prompts[0]
    assert "`t1` \"Book the dentist\"" in prompt and "`t2` \"Call the bank\"" in prompt
    assert "the cleaning" in prompt
    # Unattended: a permission prompt auto-denies rather than waking a phone.
    assert turn.source == "cron" and turn.in_conversation
    assert turn.label == todo_reminders.REMINDER_LABEL
    # Marlow wrote it, so the server adds nothing.
    assert posted == []
    assert [p.type for p in pushes] == [PushType.TURN_READY]


@pytest.mark.parametrize("reply", [_reply("[silent]"), _reply(""), _reply(None), _reply("boom", is_error=True)])
def test_a_turn_that_delivers_nothing_falls_back_to_the_plain_titles(store, clock, monkeypatch, pushes, reply) -> None:
    _due_two(store, clock)
    _, posted, _ = _run(store, clock, monkeypatch, reply)
    assert posted == ["Reminder: Book the dentist", "Reminder: Call the bank"]
    notify = [p for p in pushes if p.type == PushType.NOTIFY]
    assert [p.alert_body for p in notify] == posted


def test_staying_quiet_is_allowed_only_by_changing_the_list(store, clock, monkeypatch, pushes) -> None:
    _due_two(store, clock)

    def during():
        store.done("t1")

    _, posted, _ = _run(store, clock, monkeypatch, _reply("[silent]"), during=during)
    assert posted == ["Reminder: Call the bank"]


def test_a_delivered_reminder_renudges_a_day_later_unless_turned_off(store, clock, monkeypatch, pushes) -> None:
    _due_two(store, clock)
    _run(store, clock, monkeypatch, _reply("Both due."))
    clock.at += timedelta(hours=24)
    prompts, _, _ = _run(store, clock, monkeypatch, _reply("Still due."))
    assert len(prompts) == 1
    assert "reminded before" in prompts[0][0]
    preferences_store.update(renudge=False)
    clock.at += timedelta(hours=24)
    prompts, _, turn = _run(store, clock, monkeypatch, _reply("x"))
    assert turn is None


def test_a_reminder_queued_when_the_server_stopped_fires_on_start(store, clock, monkeypatch, pushes) -> None:
    _due_two(store, clock)
    store.mark_fired(["t1", "t2"])  # queued, then the process died
    todo_reminders.recover()
    prompts, _, turn = _run(store, clock, monkeypatch, _reply("Due."))
    assert len(prompts) == 1 and turn is not None


def test_the_prompt_keeps_list_text_inside_its_block(store, clock, monkeypatch, pushes) -> None:
    store.add("Pay ] the [bill", notes="line one\nline two ]", due="2026-11-04 11:00", source="user")
    clock.at = T0 + timedelta(hours=1)
    prompts, _, _ = _run(store, clock, monkeypatch, _reply("ok"))
    item_lines = [line for line in prompts[0][0].splitlines() if line.startswith("- `t1`")]
    assert len(item_lines) == 1
    assert "]" not in item_lines[0] and "[" not in item_lines[0]


def test_the_server_runs_the_reminder_loop_while_it_is_up() -> None:
    from fastapi.testclient import TestClient

    from server.app import public_app

    with TestClient(public_app):
        assert todo_reminders.reminder_watcher._task is not None
    assert todo_reminders.reminder_watcher._task is None
