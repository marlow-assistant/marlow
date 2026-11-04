"""The phone's view of the to-do list: GET /v1/todos, the taps, the re-nudge
preference and "talk about it" (docs/tasks/todos-and-reminders.md, part 2)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from server.app import public_app
from server.config import settings
from server.preferences import preferences_store
from server.queue import work_queue
from server.todos import todo_store


@pytest.fixture(autouse=True)
def empty_list():
    for path in (settings.todos_path, settings.todo_reminders_path):
        path.unlink(missing_ok=True)
    preferences_store._reset_for_tests()
    yield
    for path in (settings.todos_path, settings.todo_reminders_path):
        path.unlink(missing_ok=True)
    preferences_store._reset_for_tests()


@pytest.fixture
def headers(paired_device_token):
    return {"Authorization": f"Bearer {paired_device_token}"}


def test_the_list_needs_a_device_token() -> None:
    with TestClient(public_app) as client:
        assert client.get("/v1/todos").status_code == 401
        assert client.post("/v1/todos/t1/done").status_code == 401


def test_the_list_carries_the_facts_the_phone_groups_by(headers) -> None:
    todo_store.add("Book the dentist", notes="the cleaning", due="2030-11-06 09:00", source="user")
    todo_store.add("Pay the water bill", source="inferred")
    with TestClient(public_app) as client:
        todos = client.get("/v1/todos", headers=headers).json()["todos"]
    assert [t["id"] for t in todos] == ["t1", "t2"]
    first = todos[0]
    assert set(first) == {"id", "title", "notes", "due", "state", "source", "created", "closed", "snoozed_until"}
    assert (first["state"], first["notes"], first["snoozed_until"]) == ("open", "the cleaning", None)
    assert todos[1]["state"] == "proposed"


def test_an_empty_list_is_an_empty_array(headers) -> None:
    with TestClient(public_app) as client:
        assert client.get("/v1/todos", headers=headers).json() == {"todos": []}


@pytest.mark.parametrize("action,state", [("done", "done"), ("drop", "dropped"), ("accept", "open")])
def test_a_tap_changes_the_state_and_returns_the_item(headers, action, state) -> None:
    todo_store.add("x", source="inferred")
    with TestClient(public_app) as client:
        reply = client.post(f"/v1/todos/t1/{action}", headers=headers)
    assert reply.status_code == 200
    assert reply.json()["todo"]["state"] == state
    assert todo_store.get("t1")["state"] == state


def test_an_unknown_id_is_404_and_a_wrong_state_409(headers) -> None:
    todo_store.add("x", source="user")
    with TestClient(public_app) as client:
        assert client.post("/v1/todos/t9/done", headers=headers).status_code == 404
        assert client.post("/v1/todos/bogus/done", headers=headers).status_code == 404
        reply = client.post("/v1/todos/t1/accept", headers=headers)
    assert reply.status_code == 409
    assert "not a proposal" in reply.json()["detail"]


def test_snooze_takes_an_instant(headers) -> None:
    todo_store.add("x", source="user")
    until = (datetime.now(timezone(timedelta(hours=-5))) + timedelta(days=2)).replace(second=0, microsecond=0)
    with TestClient(public_app) as client:
        reply = client.post("/v1/todos/t1/snooze", json={"until": until.isoformat()}, headers=headers)
        assert reply.status_code == 200
        assert reply.json()["todo"]["snoozed_until"] == until.isoformat()
        # A bare local time means nothing from a phone: it sends the instant.
        bad = client.post("/v1/todos/t1/snooze", json={"until": "2030-01-02 09:00"}, headers=headers)
        assert bad.status_code == 422
        past = client.post("/v1/todos/t1/snooze", json={"until": "2020-01-02T09:00:00-05:00"}, headers=headers)
        assert past.status_code == 409


def test_renudge_is_a_shared_preference(headers) -> None:
    with TestClient(public_app) as client:
        assert client.get("/v1/preferences", headers=headers).json()["renudge"] is True
        assert client.post("/v1/preferences", json={"renudge": False}, headers=headers).status_code == 200
        assert client.get("/v1/preferences", headers=headers).json()["renudge"] is False
    assert preferences_store.renudge() is False


def _capture_prompts(monkeypatch):
    prompts = []
    real = work_queue.enqueue

    def capture(prompt, **kwargs):
        prompts.append(prompt)
        return real(prompt, **kwargs)

    monkeypatch.setattr(work_queue, "enqueue", capture)
    return prompts


def test_talking_about_an_item_puts_it_in_the_prompt_and_quotes_it(headers, monkeypatch) -> None:
    todo_store.add("Book the dentist", notes="ask about\nthe [cleaning]", due="2030-11-06", source="user")
    prompts = _capture_prompts(monkeypatch)
    with TestClient(public_app) as client:
        reply = client.post(
            "/v1/message",
            json={"text": "Can you find a slot?", "client_msg_id": "todo-1", "todo_id": "t1"},
            headers=headers,
        )
        assert reply.status_code == 202
        rows = client.get("/v1/transcript", headers=headers).json()["rows"]
    context, _, text = prompts[0].partition("\n\n")
    assert text == "Can you find a slot?"
    assert context.startswith("[To-do: the user tapped `t1` on their to-do list, \"Book the dentist\" (open, due 2030-11-06).")
    # The notes can't close the block or start a line of their own.
    assert "\n" not in context and context.count("]") == 1 and context.endswith("]")
    assert rows[0]["quote"] == {"todo_id": "t1", "emoji": "☑️", "title": "Book the dentist"}


def test_talking_about_an_unknown_item_still_sends_the_message(headers, monkeypatch) -> None:
    prompts = _capture_prompts(monkeypatch)
    with TestClient(public_app) as client:
        reply = client.post(
            "/v1/message", json={"text": "hm", "client_msg_id": "todo-2", "todo_id": "t9"}, headers=headers
        )
    assert reply.status_code == 202
    assert prompts == ["hm"]
