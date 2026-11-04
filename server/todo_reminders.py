"""Reminders derived from the to-do list (docs/tasks/todos-and-reminders.md,
part 2, decided by Mike 2026-11-04).

An open item with a due TIME, or a snooze, starts a Marlow turn when it comes
due, with the item in the prompt, so the reminder can carry what Marlow knows
around it and runs inside the conversation (which also means the reply is
something the next turn has seen: failure 1 doesn't arise for these). Items
due together share one turn.

THE SERVER KEEPS THE GUARANTEE. If that turn errors, or replies nothing or
`[silent]`, the server posts each item's title as a plain notice and pushes
it, unless the turn changed the item on the list (done, dropped, snoozed or
moved). So Marlow can skip a reminder only by a state change the user can
see, never by staying quiet. `TodoStore.settle` decides which; the queue calls
`settle_turn` when a reminder turn ends.

A loop, not crontab lines: every piece of state is already the server's
(server/todos.py), and a once-a-minute pass keeps due items out of the
schedule store's caps and its render on every change. Its cost is a minute of
lateness at most, and a reminder that came due while the server was down fires
when it comes back.
"""
from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

from server.log import get_logger
from server.preferences import preferences_store
from server.todos import todo_store

if TYPE_CHECKING:
    from server.queue import WorkQueue

logger = get_logger(__name__)

TICK_SECONDS = 60
# The Activity screen's name for these turns. Never a schedules.json id: the
# label of an entry is model-chosen, so the queue keys the fallback off the
# turn's `todo_reminder` field, not off this.
REMINDER_LABEL = "to-do reminder"

# List text was written by the model, maybe from mail. One line each, and no
# brackets, so it can't close the block or pose as a line of its own.
_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f\[\]]")


def _flat(text: str) -> str:
    return " ".join(_UNSAFE.sub(" ", text).split())


def reminder_prompt(items: list[dict[str, Any]]) -> str:
    from server.todo_tools import format_when

    lines = []
    for item in items:
        facts = []
        if item.get("due"):
            facts.append(f"due {format_when(item['due'])}")
        if item.get("reminded_before"):
            facts.append("reminded before, not yet acted on")
        lines.append(_item_line(item, facts))
    one = len(items) == 1
    return (
        f"[To-do reminder: {'this item' if one else 'these items'} on the user's list "
        f"{'is' if one else 'are'} due now.\n"
        + "\n".join(lines)
        + "\nRemind them in a line or two, with anything you know that bears on it. "
        "If you know one is already done, mark it with todo_done. Replying [silent] without "
        "changing an item on the list makes the server send them its title anyway.]"
    )


# A brief lists every open item, and notes run to 2000 characters each; a long
# list at full length would be most of the prompt. The tools give the rest.
BRIEFING_NOTES_CHARS = 300


def _item_line(item: dict[str, Any], facts: list[str], *, notes_limit: int | None = None) -> str:
    text = f"- `{item['id']}` \"{_flat(item['title'])}\""
    if facts:
        text += f" ({'; '.join(facts)})"
    if item.get("notes"):
        notes = _flat(item["notes"])
        if notes_limit is not None and len(notes) > notes_limit:
            notes = notes[:notes_limit].rstrip() + "…"
        text += f". Notes: {notes}"
    return text


# The jobs that read the list (docs/tasks/todos-and-reminders.md: "briefings
# read the store, not prose"). Shipped job names only: an entry's id is
# model-chosen, so app.py applies this to `job` dispatches, never `entry`.
BRIEFING_JOBS: frozenset[str] = frozenset({"morning", "week-ahead"})


def briefing_block() -> str:
    """The open and proposed items, for a briefing job's prompt. The server
    fixes which items the brief works from; what it says about them stays
    Marlow's judgement. Says so when the list is empty, so the brief doesn't
    go looking for to-dos in memory prose instead."""
    from server.todo_tools import format_when

    active = [i for i in todo_store.client_items() if i["state"] in ("open", "proposed")]
    if not active:
        return "[To-do list: the user's to-do list is empty.]"
    lines = []
    for item in (i for i in active if i["state"] == "open"):
        facts = [f"due {format_when(item['due'])}"] if item.get("due") else []
        if todo_store.is_overdue(item):
            facts.append("overdue")
        if item.get("snoozed_until"):
            facts.append(f"snoozed until {format_when(item['snoozed_until'])}")
        lines.append(_item_line(item, facts, notes_limit=BRIEFING_NOTES_CHARS))
    proposed = [
        _item_line(i, [], notes_limit=BRIEFING_NOTES_CHARS) for i in active if i["state"] == "proposed"
    ]
    if proposed:
        lines.append("Proposed by you, not yet accepted (they don't remind):")
        lines.extend(proposed)
    return (
        "[To-do list, from the store (the server's copy is the list; memory notes "
        "about to-dos may be stale):\n" + "\n".join(lines) + "]"
    )


def stale_proposals_sentence() -> str:
    """For a chat turn: proposals unconfirmed for two days, each raised once
    more (Mike, 2026-11-04). Consumes them, so the caller only calls it on
    a turn the user is present for. Never raises."""
    try:
        items = todo_store.take_stale_proposals()
    except Exception:
        logger.exception("could not read stale to-do proposals")
        return ""
    if not items:
        return ""
    named = ", ".join(f"`{i['id']}` \"{_flat(i['title'])}\"" for i in items)
    return (
        f" You proposed these to-dos two or more days ago and they're still unconfirmed: {named}."
        " If it fits this conversation, ask once whether to keep them (todo_accept) or drop them"
        " (todo_drop); after this they stay on the list without being raised again."
    )


async def tick(queue: "WorkQueue") -> str | None:
    """One pass: queue a reminder turn for whatever is due. Returns its turn
    id, or None when nothing was. Store reads and writes run in a thread;
    enqueue runs on the loop, as it must."""
    # Both reads in the thread: the preference is a file too.
    items = await asyncio.to_thread(lambda: todo_store.due_for_reminder(renudge=preferences_store.renudge()))
    if not items:
        return None
    fired = await asyncio.to_thread(todo_store.mark_fired, [i["id"] for i in items])
    logger.info("to-do reminder for %s", ", ".join(fired))
    return queue.enqueue(
        reminder_prompt(items),
        # Unattended: nobody asked for this turn, so a permission request
        # auto-denies rather than waking a phone (active_turn).
        source="cron",
        in_conversation=True,
        label=REMINDER_LABEL,
        todo_reminder=fired,
    )


def settle_turn(fired: dict[str, str], *, delivered: bool) -> list[str]:
    """The notices the server must still send for a reminder turn that just
    ended: one per item it didn't deliver and didn't deal with. Never raises:
    a failure here is logged and costs the fallback, not the queue."""
    try:
        return [f"Reminder: {item['title']}" for item in todo_store.settle(fired, delivered=delivered)]
    except Exception:
        logger.exception("could not settle the to-do reminder for %s", ", ".join(fired))
        return []


def recover() -> None:
    try:
        todo_store.recover()
    except Exception:
        logger.exception("to-do reminder recovery failed")


class ReminderWatcher:
    """Owns the loop's task, like auth_monitor's watcher."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    def start(self, queue: "WorkQueue") -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(queue))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self, queue: "WorkQueue") -> None:
        await asyncio.to_thread(recover)
        while True:
            try:
                await tick(queue)
            except Exception:
                # One bad pass must not end the reminders.
                logger.exception("to-do reminder pass failed")
            await asyncio.sleep(TICK_SECONDS)


reminder_watcher = ReminderWatcher()
