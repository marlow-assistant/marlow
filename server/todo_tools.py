"""The to-do tools, answered by the server (docs/tasks/todos-and-reminders.md).

server/todos_mcp.py is the MCP face the CLI talks to; it relays every call
here over the internal socket and adds nothing. So the arguments are checked
here, against `ARGUMENTS`, and the words the model reads back are written
here, next to the store they describe. A refusal is an error result with
`TodoError`'s reason, which is worded to be acted on.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable

from server.log import get_logger
from server.preferences import preferences_store
from server.todos import TodoError, due_instant, todo_store

logger = get_logger(__name__)

# name -> {argument: required}. todos_mcp.TOOLS mirrors this; a test holds
# them together.
ARGUMENTS: dict[str, dict[str, bool]] = {
    "todo_add": {"title": True, "notes": False, "due": False, "source": True},
    "todo_update": {"id": True, "title": False, "notes": False, "due": False},
    "todo_done": {"id": True},
    "todo_drop": {"id": True},
    "todo_accept": {"id": True},
    "todo_snooze": {"id": True, "until": True},
    "todo_list": {"state": False},
    "todo_renudge": {"on": True},
}

LIST_STATES = ("active", "done", "dropped", "all")


def format_when(text: str) -> str:
    """A due or snooze as the user would read it, in their zone."""
    moment = due_instant(text)
    if moment is None:
        try:
            return date.fromisoformat(text).strftime("%a %Y-%m-%d")
        except ValueError:
            return text
    return moment.astimezone(todo_store.zone()).strftime("%a %Y-%m-%d %H:%M")


def line(item: dict[str, Any], *, notes: bool = False) -> str:
    parts = [item["id"], item["state"], item["title"]]
    if item.get("due"):
        parts.append(f"due {format_when(item['due'])}")
    if item.get("snoozed_until"):
        parts.append(f"snoozed until {format_when(item['snoozed_until'])}")
    text = " · ".join(parts)
    if notes and item.get("notes"):
        text += "\n  notes: " + item["notes"].replace("\n", "\n  ")
    return text


def _add(a: dict[str, Any]) -> str:
    item = todo_store.add(a["title"], notes=a.get("notes"), due=a.get("due"), source=a["source"])
    text = f"Added {item['id']} ({item['state']}): {item['title']}"
    if item["due"]:
        text += f" · due {format_when(item['due'])}"
    if item["state"] == "proposed":
        text += (" — a proposal: it won't be reminded until they accept it."
                 " If it matters, ask them about it now.")
    elif due_instant(item["due"]) is None:
        text += " — no time, so no reminder of its own; it shows on their list and in the brief."
    return text


def _update(a: dict[str, Any]) -> str:
    fields = {k: a[k] for k in ("title", "notes", "due") if k in a}
    if not fields:
        raise TodoError("nothing to change: give title, notes or due")
    return "Updated: " + line(todo_store.update(a["id"], **fields))


def _done(a: dict[str, Any]) -> str:
    return "Done: " + line(todo_store.done(a["id"]))


def _drop(a: dict[str, Any]) -> str:
    return "Dropped (kept, so it isn't proposed again): " + line(todo_store.drop(a["id"]))


def _accept(a: dict[str, Any]) -> str:
    return "Accepted, now open: " + line(todo_store.accept(a["id"]))


def _snooze(a: dict[str, Any]) -> str:
    return "Snoozed: " + line(todo_store.snooze(a["id"], a["until"]))


def _list(a: dict[str, Any]) -> str:
    which = a.get("state", "active")
    if which not in LIST_STATES:
        raise TodoError(f"state must be one of {', '.join(LIST_STATES)}")
    items = todo_store.items()
    if which == "active":
        items = [i for i in items if i["state"] in ("open", "proposed")]
    elif which != "all":
        items = [i for i in items if i["state"] == which]
    if not items:
        return "No to-dos open or proposed." if which == "active" else f"No to-dos ({which})."
    return "\n".join(line(i, notes=True) for i in items)


def _renudge(a: dict[str, Any]) -> str:
    if not isinstance(a["on"], bool):
        raise TodoError("on must be true or false")
    preferences_store.update(renudge=a["on"])
    if a["on"]:
        return "Daily re-reminders are on: an unanswered reminder comes back a day later until it's done, dropped or snoozed."
    return "Daily re-reminders are off: each reminder fires once, then the item just sits on the list and in the brief. Settings has the same switch."


HANDLERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "todo_add": _add,
    "todo_update": _update,
    "todo_done": _done,
    "todo_drop": _drop,
    "todo_accept": _accept,
    "todo_snooze": _snooze,
    "todo_list": _list,
    "todo_renudge": _renudge,
}


def call(name: Any, arguments: Any) -> tuple[str, bool]:
    """(text for the model, is_error). Never raises."""
    if name not in HANDLERS:
        return f"unknown to-do tool {name!r}", True
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return "arguments must be an object", True
    known = ARGUMENTS[name]
    extra = sorted(set(arguments) - set(known))
    if extra:
        return f"unknown argument {extra[0]!r}; {name} takes {', '.join(known)}", True
    missing = [k for k, required in known.items() if required and k not in arguments]
    if missing:
        return f"{name} needs {', '.join(missing)}", True
    try:
        return HANDLERS[name](arguments), False
    except TodoError as e:
        return str(e), True
    except Exception as e:  # a bug costs one answer, not the turn
        logger.exception("to-do tool %s failed", name)
        return f"internal error ({type(e).__name__}); nothing may have changed", True
