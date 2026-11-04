"""The to-do tools for Marlow: a stdio MCP server that relays every call to
the server's internal API (docs/tasks/todos-and-reminders.md, part 2).

THIS PROCESS DECIDES NOTHING AND TOUCHES NO FILE. It offers the tool schemas,
POSTs each call to `/internal/todos/tool` over the instance's Unix socket and
hands back the answer. server/todo_tools.py checks the arguments and
server/todos.py is the one writer, so a tool call, a tap on the phone and the
reminder loop all meet on the same lock, and the model can neither invent an
id nor write a list the server would refuse.

Run as `python -I <this file> --socket <path>` (invoke.py), like the weather
and onboarding servers: the CLI spawns it with cwd = the agent-writable
working dir, and isolated mode keeps that directory off sys.path. The one
import of ours, the socket client, comes from this file's own checkout, put
on the path explicitly below; that checkout is the server's code, and the
agent writing to it would already be the whole game.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.internal_client import post_json  # noqa: E402

SERVER_INFO = {"name": "todos", "version": "1"}
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
ENDPOINT = "/internal/todos/tool"
# The store answers from memory and a file; anything near this is a wedged
# server, and the turn should hear so rather than wait.
TIMEOUT_SECONDS = 15.0

_ID = {"type": "string", "description": "the to-do's id, like t12"}
_DUE = {
    "type": ["string", "null"],
    "description": (
        "YYYY-MM-DD for a day, or YYYY-MM-DD HH:MM for a time, in the user's local time. "
        "Only a time gets a reminder of its own; give one when they want reminding then. "
        "null clears it."
    ),
}


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


# The wording is behaviour: it is most of what the model knows about the list
# on a turn that never reads the identity's To-dos section closely.
TOOLS: list[dict] = [
    _tool(
        "todo_add",
        "Add a to-do to the user's list. source=user when they asked for it in their own words: it "
        "is open at once. source=inferred when you noticed it yourself (mail, the calendar, a "
        "conversation): it is a proposal, never reminded, until they accept it. If an inferred "
        "one looks important, ask them about it in this reply. The store assigns the id.",
        {
            "title": {"type": "string", "description": "short, one line: what to do"},
            "notes": {"type": "string", "description": "why, context, links to topic files"},
            "due": _DUE,
            "source": {"type": "string", "enum": ["user", "inferred"]},
        },
        ["title", "source"],
    ),
    _tool(
        "todo_update",
        "Change an open or proposed to-do's title, notes or due. A new due time is a new reminder.",
        {"id": _ID, "title": {"type": "string"}, "notes": {"type": ["string", "null"]}, "due": _DUE},
        ["id"],
    ),
    _tool("todo_done", "Mark a to-do done. Done is done: don't follow up on it later.", {"id": _ID}, ["id"]),
    _tool(
        "todo_drop",
        "Drop a to-do the user won't do (\"not gonna happen\", or a proposal they declined). "
        "It is kept so it isn't proposed again.",
        {"id": _ID},
        ["id"],
    ),
    _tool("todo_accept", "The user confirmed a proposed to-do: make it open.", {"id": _ID}, ["id"]),
    _tool(
        "todo_snooze",
        "Hold an open to-do's reminder until a time (\"remind me in two hours\", \"tomorrow\"). "
        "It fires then, even if it had no due time.",
        {"id": _ID, "until": {"type": "string", "description": "YYYY-MM-DD HH:MM, the user's local time"}},
        ["id", "until"],
    ),
    _tool(
        "todo_list",
        "The to-do list. active (the default) is open and proposed, with notes.",
        {"state": {"type": "string", "enum": ["active", "done", "dropped", "all"]}},
        [],
    ),
    _tool(
        "todo_renudge",
        "Turn daily re-reminders on or off for this user. On (the default), a reminder they don't "
        "act on comes back a day later until the to-do is done, dropped or snoozed. Turn it off "
        "if they say they're being nagged; Settings has the same switch.",
        {"on": {"type": "boolean"}},
        ["on"],
    ),
]
_NAMES = frozenset(t["name"] for t in TOOLS)

Post = Callable[[str, dict], Any]


def handle(message: Any, post: Post) -> dict | None:
    """One JSON-RPC message in, one response out (None for a notification)."""
    if not isinstance(message, dict) or "id" not in message:
        return None
    msg_id, method = message["id"], message.get("method")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}

    def ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def tool_result(text: str, is_error: bool) -> dict:
        return ok({"content": [{"type": "text", "text": text}], "isError": is_error})

    if method == "initialize":
        asked = params.get("protocolVersion")
        return ok({
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        if name not in _NAMES:
            return {"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32602, "message": f"unknown tool {name!r}"}}
        try:
            answer = post(ENDPOINT, {"name": name, "arguments": params.get("arguments", {})})
            text, is_error = answer["text"], answer["is_error"]
            if not isinstance(text, str) or not isinstance(is_error, bool):
                raise ValueError("malformed answer")
        except OSError as e:
            return tool_result(f"To-do error: couldn't reach the Marlow server ({e}); nothing changed.", True)
        except Exception as e:
            return tool_result(f"To-do error: bad answer from the server ({type(e).__name__}).", True)
        return tool_result(text, is_error)
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"unknown method {method!r}"}}


def _socket_path(argv: list[str]) -> Path | None:
    if "--socket" in argv and argv.index("--socket") + 1 < len(argv):
        return Path(argv[argv.index("--socket") + 1])
    return None


def main() -> None:
    socket_path = _socket_path(sys.argv[1:])

    def post(path: str, body: dict) -> Any:
        if socket_path is None:
            raise OSError("no --socket given")
        return post_json(socket_path, path, body, timeout=TIMEOUT_SECONDS)

    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            response = handle(message, post)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
