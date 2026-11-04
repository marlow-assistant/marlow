"""The to-do tools as the server answers them: server/todo_tools.py behind
`/internal/todos/tool`, and server/todos_mcp.py, which only relays
(docs/tasks/todos-and-reminders.md, part 2)."""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from server import todo_tools, todos, todos_mcp
from server.app import internal_app
from server.preferences import preferences_store
from server.todos import TodoStore

ZONE = ZoneInfo("America/Toronto")
T0 = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)
SCRIPT = Path(__file__).resolve().parent.parent / "server" / "todos_mcp.py"


@pytest.fixture
def store(tmp_path, monkeypatch) -> TodoStore:
    s = TodoStore(
        path=tmp_path / "todos.json",
        state_path=tmp_path / "state.json",
        clock=lambda: T0,
        zone=lambda: ZONE,
    )
    monkeypatch.setattr(todos, "todo_store", s)
    monkeypatch.setattr(todo_tools, "todo_store", s)
    preferences_store._reset_for_tests()
    yield s
    preferences_store._reset_for_tests()


def _call(name, **arguments):
    return todo_tools.call(name, arguments)


def test_add_then_list(store) -> None:
    text, err = _call("todo_add", title="Book the dentist", due="2026-11-06 09:00", source="user")
    assert not err
    assert text.startswith("Added t1 (open)")
    text, err = _call("todo_list")
    assert not err
    assert "t1 · open · Book the dentist · due Fri 2026-11-06 09:00" in text


def test_an_inferred_add_says_it_is_a_proposal_and_wont_remind(store) -> None:
    text, _ = _call("todo_add", title="Pay the water bill", source="inferred")
    assert "proposed" in text and "won't be reminded" in text


def test_a_refusal_is_an_error_result_with_the_reason(store) -> None:
    text, err = _call("todo_add", title="x", due="next week", source="user")
    assert err and "YYYY-MM-DD" in text


def test_unknown_arguments_and_tools_are_refused(store) -> None:
    text, err = _call("todo_add", title="x", source="user", priority=1)
    assert err and "priority" in text
    text, err = todo_tools.call("todo_explode", {})
    assert err and "todo_explode" in text
    text, err = todo_tools.call("todo_list", "not an object")
    assert err


def test_update_can_clear_the_due(store) -> None:
    _call("todo_add", title="x", due="2026-11-06", source="user")
    text, err = _call("todo_update", id="t1", due=None)
    assert not err and "due" not in text.split("·", 3)[-1]
    assert store.get("t1")["due"] is None


def test_done_drop_accept_snooze(store) -> None:
    for title in ("a", "b"):
        _call("todo_add", title=title, source="user")
    _call("todo_add", title="c", source="inferred")
    assert not _call("todo_done", id="t1")[1]
    assert not _call("todo_drop", id="t2")[1]
    assert not _call("todo_accept", id="t3")[1]
    text, err = _call("todo_snooze", id="t3", until="2026-11-05 09:00")
    assert not err and "snoozed until Thu 2026-11-05 09:00" in text


def test_list_filters_by_state(store) -> None:
    _call("todo_add", title="a", source="user")
    _call("todo_add", title="b", source="user")
    _call("todo_done", id="t2")
    active, _ = _call("todo_list")
    done, _ = _call("todo_list", state="done")
    assert "t1" in active and "t2" not in active
    assert "t2" in done and "t1" not in done
    assert _call("todo_list", state="everything")[1]


def test_an_empty_list_says_so(store) -> None:
    assert _call("todo_list")[0] == "No to-dos open or proposed."


def test_renudge_is_the_users_preference(store) -> None:
    assert preferences_store.renudge() is True
    text, err = _call("todo_renudge", on=False)
    assert not err and "off" in text
    assert preferences_store.renudge() is False
    assert _call("todo_renudge", on="no")[1]


def test_the_internal_endpoint_answers_a_tool_call(store) -> None:
    with TestClient(internal_app) as client:
        reply = client.post("/internal/todos/tool", json={"name": "todo_add", "arguments": {"title": "x", "source": "user"}})
        assert reply.status_code == 200
        assert reply.json() == {
            "text": "Added t1 (open): x — no time, so no reminder of its own; it shows on their list and in the brief.",
            "is_error": False,
        }
        reply = client.post("/internal/todos/tool", json={"name": "todo_done", "arguments": {"id": "t9"}})
        assert reply.json()["is_error"] is True


# --- the MCP relay ----------------------------------------------------------------


def test_the_relay_offers_exactly_the_tools_the_server_answers() -> None:
    assert [t["name"] for t in todos_mcp.TOOLS] == list(todo_tools.HANDLERS)
    for tool in todos_mcp.TOOLS:
        schema = tool["inputSchema"]
        assert schema["additionalProperties"] is False
        assert set(schema["properties"]) == set(todo_tools.ARGUMENTS[tool["name"]])


def test_the_relay_passes_the_call_through_and_the_answer_back() -> None:
    seen = []

    def post(path, body):
        seen.append((path, body))
        return {"text": "Added t1 (open): x", "is_error": False}

    reply = todos_mcp.handle(
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
         "params": {"name": "todo_add", "arguments": {"title": "x", "source": "user"}}},
        post,
    )
    assert seen == [("/internal/todos/tool", {"name": "todo_add", "arguments": {"title": "x", "source": "user"}})]
    assert reply["result"] == {"content": [{"type": "text", "text": "Added t1 (open): x"}], "isError": False}


def test_an_unreachable_server_is_one_error_answer() -> None:
    def post(path, body):
        raise OSError("connection refused")

    reply = todos_mcp.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "todo_list", "arguments": {}}},
        post,
    )
    assert reply["result"]["isError"] is True
    assert "couldn't reach" in reply["result"]["content"][0]["text"]


def test_an_unknown_tool_is_a_protocol_error_without_a_call() -> None:
    def post(path, body):  # pragma: no cover - must not run
        raise AssertionError("relayed an unknown tool")

    reply = todos_mcp.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "Bash", "arguments": {}}}, post
    )
    assert reply["error"]["code"] == -32602


def test_stdio_round_trip_to_a_real_socket(tmp_path) -> None:
    """The real file, run as invoke.py runs it (`python -I`, cwd elsewhere),
    talking HTTP over a Unix socket."""
    # AF_UNIX paths are short (104 bytes on macOS); pytest's tmp_path isn't.
    sock_path = Path(tempfile.mkdtemp(dir="/tmp")) / "s.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(sock_path))
    listener.listen(1)
    got = {}

    def serve():
        conn, _ = listener.accept()
        data = b""
        while b"\r\n\r\n" not in data:
            data += conn.recv(4096)
        head, _, body = data.partition(b"\r\n\r\n")
        length = int([h for h in head.split(b"\r\n") if h.lower().startswith(b"content-length")][0].split(b":")[1])
        while len(body) < length:
            body += conn.recv(4096)
        got["request"] = head.split(b"\r\n")[0].decode()
        got["body"] = json.loads(body)
        payload = json.dumps({"text": "No to-dos open or proposed.", "is_error": False}).encode()
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                     + str(len(payload)).encode() + b"\r\nConnection: close\r\n\r\n" + payload)
        conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    lines = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "todo_list", "arguments": {}}}),
    ]) + "\n"
    done = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--socket", str(sock_path)],
        input=lines, capture_output=True, text=True, timeout=20, cwd=tmp_path,
    )
    thread.join(5)
    listener.close()
    assert done.returncode == 0, done.stderr
    replies = [json.loads(line) for line in done.stdout.splitlines()]
    assert replies[0]["result"]["serverInfo"]["name"] == "todos"
    assert replies[1]["result"]["content"][0]["text"] == "No to-dos open or proposed."
    assert got["request"] == "POST /internal/todos/tool HTTP/1.1"
    assert got["body"] == {"name": "todo_list", "arguments": {}}


def test_the_relay_imports_only_the_socket_client_of_ours() -> None:
    source = SCRIPT.read_text()
    ours = [line for line in source.splitlines() if line.startswith(("from server", "import server"))]
    assert ours == ["from server.internal_client import post_json  # noqa: E402"]


# --- policy lockstep -----------------------------------------------------------


def test_every_tool_is_vetted_and_allowed() -> None:
    """Cron turns use them (a reminder turn marks an item done), and an
    unvetted tool auto-denies there."""
    from server import boundaries

    names = {f"mcp__todos__{t['name']}" for t in todos_mcp.TOOLS}
    vetted = {t for t in boundaries.VETTED_PATHLESS_TOOLS if t.startswith("mcp__todos__")}
    assert vetted == names
    policy = json.loads((Path(__file__).resolve().parent.parent / "deploy" / "marlow-settings.json").read_text())
    assert names <= set(policy["permissions"]["allow"])
