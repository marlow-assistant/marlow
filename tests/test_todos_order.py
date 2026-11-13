"""The to-do list's order (docs/tasks/todos-and-reminders.md, "Manual order").

The order is the file's own: `items`, top first, for every client and tool. A
list nobody has dragged stays in due order, because each item that arrives is
slotted by its due; a drag then holds until that item's due changes.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from server import boundaries, todo_reminders, todo_tools, todos, todos_mcp
from server.app import public_app
from server.config import settings
from server.todos import TodoError, TodoNotFound, TodoStore, todo_store

ZONE = ZoneInfo("America/Toronto")
# A Wednesday, 10:00 in Toronto.
T0 = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)
SETTINGS_FILE = Path(__file__).resolve().parent.parent / "deploy" / "marlow-settings.json"


@pytest.fixture
def store(tmp_path, monkeypatch) -> TodoStore:
    s = TodoStore(
        path=tmp_path / "todos" / "todos.json",
        state_path=tmp_path / "data" / "todo_reminders.json",
        clock=lambda: T0,
        zone=lambda: ZONE,
    )
    monkeypatch.setattr(todos, "todo_store", s)
    monkeypatch.setattr(todo_tools, "todo_store", s)
    monkeypatch.setattr(todo_reminders, "todo_store", s)
    return s


def _on_disk(store: TodoStore) -> dict:
    return json.loads(store.path.read_text())


def _titles(store: TodoStore) -> list[str]:
    return [i["title"] for i in store.items() if i["state"] == "open"]


def _ids(store: TodoStore) -> list[str]:
    return [i["id"] for i in store.items() if i["state"] == "open"]


# --- arriving --------------------------------------------------------------------

def test_an_undated_item_joins_the_bottom(store) -> None:
    store.add("first", source="user")
    store.add("second", source="user")
    assert _titles(store) == ["first", "second"]


def test_a_list_nobody_dragged_stays_in_due_order(store) -> None:
    store.add("undated", source="user")
    store.add("friday", due="2026-11-06", source="user")
    store.add("next week", due="2026-11-11", source="user")
    store.add("thursday noon", due="2026-11-05 12:00", source="user")
    store.add("also undated", source="user")
    assert _titles(store) == ["thursday noon", "friday", "next week", "undated", "also undated"]


def test_equal_dues_stay_in_the_order_they_came(store) -> None:
    store.add("a", due="2026-11-06", source="user")
    store.add("b", due="2026-11-06", source="user")
    assert _titles(store) == ["a", "b"]


def test_a_day_sorts_from_its_start_in_the_users_zone(store) -> None:
    # 22:00 on the 5th in Toronto is 03:00 on the 6th in UTC: after the 6th
    # begins if the day were read as UTC, before it in the user's zone.
    store.add("the 6th", due="2026-11-06", source="user")
    store.add("late on the 5th", due="2026-11-05 22:00", source="user")
    assert _titles(store) == ["late on the 5th", "the 6th"]


def test_a_dated_item_tops_a_list_of_undated_ones(store) -> None:
    store.add("undated", source="user")
    store.add("friday", due="2026-11-06", source="user")
    assert _titles(store) == ["friday", "undated"]


def test_accepting_a_proposal_slots_it_by_its_due(store) -> None:
    store.add("friday", due="2026-11-06", source="user")
    store.add("undated", source="user")
    earlier = store.add("thursday", due="2026-11-05", source="inferred")["id"]
    plain = store.add("no date", source="inferred")["id"]
    store.accept(plain)
    store.accept(earlier)
    assert _titles(store) == ["thursday", "friday", "undated", "no date"]


def test_a_new_due_finds_its_place_again(store) -> None:
    a = store.add("a", due="2026-11-06", source="user")["id"]
    store.add("b", due="2026-11-07", source="user")
    store.add("c", due="2026-11-08", source="user")
    store.update(a, due="2026-11-20")
    assert _titles(store) == ["b", "c", "a"]
    store.update(a, due=None)
    store.add("d", due="2026-11-09", source="user")
    assert _titles(store) == ["b", "c", "d", "a"]


# --- moving ----------------------------------------------------------------------

def test_move_puts_an_item_just_above_another(store) -> None:
    a, b, c = (store.add(t, source="user")["id"] for t in "abc")
    store.move(c, a)
    assert _titles(store) == ["c", "a", "b"]
    store.move(a, c)
    assert _titles(store) == ["a", "c", "b"]


def test_move_with_no_anchor_goes_to_the_bottom(store) -> None:
    a = store.add("a", source="user")["id"]
    store.add("b", source="user")
    store.add("suggested", source="inferred")
    store.move(a, None)
    assert _titles(store) == ["b", "a"]


def test_a_move_is_on_disk(store) -> None:
    a, b = (store.add(t, source="user")["id"] for t in "ab")
    store.move(b, a)
    assert [i["title"] for i in _on_disk(store)["items"]] == ["b", "a"]


def test_a_move_above_itself_changes_nothing(store) -> None:
    a = store.add("a", source="user")["id"]
    store.add("b", source="user")
    store.move(a, a)
    assert _titles(store) == ["a", "b"]


def test_only_an_open_item_moves(store) -> None:
    a = store.add("a", source="user")["id"]
    suggested = store.add("suggested", source="inferred")["id"]
    closed = store.add("closed", source="user")["id"]
    store.done(closed)
    for item_id in (suggested, closed):
        with pytest.raises(TodoError, match="only an open to-do"):
            store.move(item_id, a)


def test_a_move_can_anchor_on_a_row_just_ticked(store) -> None:
    # A ticked row keeps its place on the phone until the pane reopens, so a
    # drag can land above it. It is still in the file, so that place is real.
    a, b, c = (store.add(t, source="user")["id"] for t in "abc")
    store.done(b)
    store.move(c, b)
    store.reopen(b)
    assert _titles(store) == ["a", "c", "b"]


def test_a_move_needs_a_real_item_and_anchor(store) -> None:
    a = store.add("a", source="user")["id"]
    with pytest.raises(TodoNotFound):
        store.move("t99", a)
    with pytest.raises(TodoNotFound):
        store.move(a, "t99")
    assert _titles(store) == ["a"]


def test_a_moved_item_stays_put_as_others_arrive(store) -> None:
    store.add("friday", due="2026-11-06", source="user")
    store.add("next week", due="2026-11-11", source="user")
    pinned = store.add("undated", source="user")["id"]
    store.move(pinned, _ids(store)[0])
    store.add("saturday", due="2026-11-07", source="user")
    store.add("thursday", due="2026-11-05", source="user")
    store.add("later", source="user")
    assert _titles(store) == ["undated", "thursday", "friday", "saturday", "next week", "later"]


def test_a_new_item_lands_after_the_last_one_due_before_it(store) -> None:
    # Not before the first one due after it: that would be the far-off item
    # someone dragged to the top, and everything new would pile up above it.
    store.add("the 10th", due="2026-11-10", source="user")
    store.add("the 20th", due="2026-11-20", source="user")
    far = store.add("december", due="2026-12-31", source="user")["id"]
    store.move(far, _ids(store)[0])
    store.add("the 15th", due="2026-11-15", source="user")
    assert _titles(store) == ["december", "the 10th", "the 15th", "the 20th"]


def test_an_item_earlier_than_all_lands_above_the_first_dated(store) -> None:
    store.add("friday", due="2026-11-06", source="user")
    pinned = store.add("undated", source="user")["id"]
    store.move(pinned, _ids(store)[0])
    store.add("thursday", due="2026-11-05", source="user")
    assert _titles(store) == ["undated", "thursday", "friday"]


def test_an_edit_that_leaves_the_due_alone_keeps_a_dragged_place(store) -> None:
    store.add("a", due="2026-11-06", source="user")
    b = store.add("b", due="2026-11-07", source="user")["id"]
    store.move(b, _ids(store)[0])
    store.update(b, title="b, renamed", due="2026-11-07")
    assert _titles(store) == ["b, renamed", "a"]


def test_a_tick_taken_back_returns_to_its_place(store) -> None:
    a, b, c = (store.add(t, source="user")["id"] for t in "abc")
    store.move(c, a)
    store.done(a)
    store.reopen(a)
    assert _titles(store) == ["c", "a", "b"]


def test_a_snooze_does_not_move_an_item(store) -> None:
    a = store.add("a", source="user")["id"]
    store.add("b", source="user")
    store.snooze(a, "2026-11-05 09:00")
    assert _titles(store) == ["a", "b"]


def test_closing_a_neighbour_leaves_the_order_alone(store) -> None:
    a, b, c = (store.add(t, source="user")["id"] for t in "abc")
    store.move(c, a)
    store.drop(b)
    assert _titles(store) == ["c", "a"]


def test_the_client_list_is_in_list_order(store) -> None:
    a, b = (store.add(t, source="user")["id"] for t in "ab")
    store.move(b, a)
    assert [i["title"] for i in store.client_items()] == ["b", "a"]


def test_the_briefing_reads_the_list_in_its_order(store) -> None:
    a = store.add("first", due="2026-11-05", source="user")["id"]
    b = store.add("second", due="2026-11-06", source="user")["id"]
    store.move(b, a)
    block = todo_reminders.briefing_block()
    assert block.index("second") < block.index("first")


# --- the upgrade: a list written before the order was the file's ------------------

def _legacy(store: TodoStore, items: list[dict]) -> None:
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps({"next_id": 20, "items": items}))


def _old_item(
    item_id: str, *, due=None, created="2026-11-01T08:00:00-05:00", state="open", closed=None
) -> dict:
    return {
        "id": item_id, "title": f"title {item_id}", "notes": None, "due": due, "state": state,
        "source": "user", "created": created, "updated": created, "closed": closed,
    }


def test_an_old_list_reads_in_the_order_the_clients_showed_it(store) -> None:
    _legacy(store, [
        _old_item("t1", created="2026-11-02T08:00:00-05:00"),
        _old_item("t2", due="2026-11-10"),
        _old_item("t3", created="2026-11-01T08:00:00-05:00"),
        _old_item("t4", due="2026-11-05T09:00:00-05:00"),
    ])
    assert _ids(store) == ["t4", "t2", "t3", "t1"]


def test_an_old_list_is_written_in_that_order_once_and_then_left_alone(store) -> None:
    _legacy(store, [_old_item("t1"), _old_item("t2", due="2026-11-10"), _old_item("t3", due="2026-11-12")])
    store.move("t3", "t2")
    assert _ids(store) == ["t3", "t2", "t1"]
    # The drag put a later due above an earlier one; sorting again would undo it.
    assert [i["id"] for i in _on_disk(store)["items"]] == ["t3", "t2", "t1"]
    fresh = TodoStore(path=store.path, state_path=store.state_path, zone=lambda: ZONE)
    assert _ids(fresh) == ["t3", "t2", "t1"]


def test_reading_an_old_list_writes_nothing(store) -> None:
    _legacy(store, [_old_item("t1"), _old_item("t2", due="2026-11-10")])
    before = store.path.read_text()
    store.items()
    assert store.path.read_text() == before


def test_the_upgrade_loses_no_item_and_keeps_the_next_id(store) -> None:
    _legacy(store, [
        _old_item("t1", due="not a date"),
        _old_item("t2", state="done", closed="2026-11-03T08:00:00-05:00"),
        _old_item("t3", due="2026-13-45"),
        _old_item("t4", due="2026-11-10"),
    ])
    assert {i["id"] for i in store.items()} == {"t1", "t2", "t3", "t4"}
    # One this can't read counts as undated, as it does on the clients.
    assert _ids(store)[0] == "t4"
    assert store.add("next", source="user")["id"] == "t20"


# --- the tools --------------------------------------------------------------------

def _call(name, **arguments):
    return todo_tools.call(name, arguments)


def test_todo_move_moves_and_says_where_it_landed(store) -> None:
    for title in ("a", "b", "c"):
        _call("todo_add", title=title, source="user")
    text, err = _call("todo_move", id="t3", before="t1")
    assert not err
    assert "1 of 3" in text
    assert _titles(store) == ["c", "a", "b"]


def test_todo_move_with_no_anchor_goes_to_the_bottom(store) -> None:
    for title in ("a", "b"):
        _call("todo_add", title=title, source="user")
    text, err = _call("todo_move", id="t1")
    assert not err and "2 of 2" in text
    assert _titles(store) == ["b", "a"]


def test_todo_move_refuses_with_the_reason(store) -> None:
    _call("todo_add", title="a", source="inferred")
    text, err = _call("todo_move", id="t1")
    assert err and "only an open to-do" in text


def test_todo_list_shows_open_items_in_their_order_then_proposals(store) -> None:
    _call("todo_add", title="a", source="user")
    _call("todo_add", title="suggested", source="inferred")
    _call("todo_add", title="b", source="user")
    _call("todo_move", id="t3", before="t1")
    text, err = _call("todo_list")
    assert not err
    assert [row.split(" · ")[0] for row in text.splitlines()] == ["b", "a", "suggested"]


def test_todo_move_is_offered_and_vetted_everywhere() -> None:
    # A tool the settings file doesn't allow is one a cron turn can't call.
    assert "todo_move" in {t["name"] for t in todos_mcp.TOOLS}
    assert "mcp__todos__todo_move" in boundaries.VETTED_PATHLESS_TOOLS
    assert "mcp__todos__todo_move" in SETTINGS_FILE.read_text()


# --- the API ----------------------------------------------------------------------

@pytest.fixture
def api_list():
    for path in (settings.todos_path, settings.todo_reminders_path):
        path.unlink(missing_ok=True)
    yield
    for path in (settings.todos_path, settings.todo_reminders_path):
        path.unlink(missing_ok=True)


@pytest.fixture
def headers(paired_device_token, api_list):
    return {"Authorization": f"Bearer {paired_device_token}"}


def _listed(client, headers) -> list[str]:
    return [t["title"] for t in client.get("/v1/todos", headers=headers).json()["todos"]]


def test_move_answers_with_the_whole_list_in_its_new_order(headers) -> None:
    a = todo_store.add("a", source="user")["id"]
    b = todo_store.add("b", source="user")["id"]
    with TestClient(public_app) as client:
        r = client.post(f"/v1/todos/{b}/move", json={"before": a}, headers=headers)
        assert r.status_code == 200
        assert [t["title"] for t in r.json()["todos"]] == ["b", "a"]
        assert _listed(client, headers) == ["b", "a"]


def test_a_null_anchor_is_the_bottom_and_a_missing_one_is_refused(headers) -> None:
    a = todo_store.add("a", source="user")["id"]
    todo_store.add("b", source="user")
    with TestClient(public_app) as client:
        # No body must not read as "to the bottom": a client bug would look like a drag.
        assert client.post(f"/v1/todos/{a}/move", json={}, headers=headers).status_code == 422
        assert _listed(client, headers) == ["a", "b"]
        assert client.post(f"/v1/todos/{a}/move", json={"before": None}, headers=headers).status_code == 200
        assert _listed(client, headers) == ["b", "a"]


def test_a_move_needs_a_device_token(api_list) -> None:
    with TestClient(public_app) as client:
        assert client.post("/v1/todos/t1/move", json={"before": None}).status_code == 401


def test_a_gone_anchor_is_404_and_a_closed_item_409(headers) -> None:
    a = todo_store.add("a", source="user")["id"]
    b = todo_store.add("b", source="user")["id"]
    todo_store.done(b)
    with TestClient(public_app) as client:
        assert client.post(f"/v1/todos/{a}/move", json={"before": "t999"}, headers=headers).status_code == 404
        assert client.post(f"/v1/todos/{b}/move", json={"before": a}, headers=headers).status_code == 409
