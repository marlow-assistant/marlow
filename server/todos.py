"""The to-do list, and the reminder bookkeeping beside it
(docs/tasks/todos-and-reminders.md, part 2).

ONE WRITER. Everything that changes the list comes through `TodoStore`: the
model's tools (server/todos_mcp.py, over the internal socket), the phone's
taps (/v1/todos) and the reminder loop (server/todo_reminders.py). They all
land on one lock in one process, so a tap can never race a turn's write, and
there is no ideas-style split into a model file and a server file. The model
never edits `todos.json`: a bad write is a refused tool call with a reason it
can act on, not a store that reads as empty, and ids are the store's, never
invented.

Two files, one lock:

- `settings.todos_path` (working tree, `todos/todos.json`): the items. Read by
  the model, the morning brief and the backup.
- `settings.todo_reminders_path` (data_dir): per item, when it last fired,
  whether that reminder is still in flight, snoozed-until and re-asked. Server
  state, per instance; part 3's shared list keeps reminders per person, and
  this is where that already lives.

Items are written before reminder state, so a crash between the two leaves an
item without bookkeeping (it fires as new), never bookkeeping for a change
that didn't land.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from server.config import settings
from server.log import get_logger

logger = get_logger(__name__)

STATES = ("proposed", "open", "done", "dropped")
ACTIVE = ("proposed", "open")
SOURCES = ("user", "inferred")

MAX_TITLE_CHARS = 200
MAX_NOTES_CHARS = 2000
# Open plus proposed. Real lists run to tens; past this is a loop adding
# proposals from a mailbox, and the refusal says so.
MAX_ACTIVE = 200
KEEP_DONE_DAYS = 30
# Kept far longer than done: a dropped item is what stops Marlow proposing the
# same thing again ("not gonna happen" is an answer, Mike 2026-11-04).
KEEP_DROPPED_DAYS = 180
RECENT_DONE_DAYS = 7
RENUDGE_AFTER = timedelta(hours=24)
REASK_AFTER = timedelta(days=2)
MAX_SNOOZE = timedelta(days=366)
# A due time this far in the past is still "now" (the tool call took a moment).
_PAST_GRACE = timedelta(minutes=1)

_ID_RE = re.compile(r"t[1-9][0-9]{0,6}\Z")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_LOCAL_TIME_RE = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})[ T]([0-9]{2}:[0-9]{2})(:00)?\Z")
# A title is one line on a phone and in a push; a newline or a control
# character would also let it start a line of its own in a turn's context.
_UNSAFE_INLINE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# Notes are prose and keep their line breaks; nothing else invisible.
_UNSAFE_NOTES = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")

DUE_FORMAT_HELP = "YYYY-MM-DD for a day, or YYYY-MM-DD HH:MM for a time (the user's local time)"

_UNSET: Any = object()


class TodoError(ValueError):
    """A refusal, worded for whoever made the call to act on."""


def user_zone() -> ZoneInfo:
    """The zone a bare local time means: the phone's, else the schedule
    store's, else the server's. Never raises."""
    from server.pairing import pairing_store
    from server.turn_context import _schedule_zone, is_valid_timezone

    try:
        zone = pairing_store.latest_timezone()
    except Exception:
        zone = None
    if not (zone and is_valid_timezone(zone)):
        zone = _schedule_zone()
    if zone:
        return ZoneInfo(zone)
    local = datetime.now().astimezone().tzinfo
    return local if isinstance(local, ZoneInfo) else ZoneInfo("UTC")


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _parse_instant(value: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo is not None else None


def due_instant(due: str | None) -> datetime | None:
    """The moment a timed due falls at; None for a date or no due."""
    if not due or _DATE_RE.fullmatch(due):
        return None
    return _parse_instant(due)


def _inline(text: str) -> str:
    return " ".join(_UNSAFE_INLINE.sub(" ", text).split())


def _clean_title(raw: Any) -> str:
    if not isinstance(raw, str):
        raise TodoError("title must be a string")
    title = _inline(raw)
    if not title:
        raise TodoError("title must not be blank")
    if len(title) > MAX_TITLE_CHARS:
        raise TodoError(f"title is over {MAX_TITLE_CHARS} characters; put the detail in notes")
    return title


def _clean_notes(raw: Any) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise TodoError("notes must be a string")
    notes = _UNSAFE_NOTES.sub(" ", raw).strip()
    if len(notes) > MAX_NOTES_CHARS:
        raise TodoError(f"notes are over {MAX_NOTES_CHARS} characters")
    return notes or None


def _same_title(a: str, b: str) -> bool:
    return " ".join(a.casefold().split()) == " ".join(b.casefold().split())


class TodoStore:
    def __init__(
        self,
        path: Path | None = None,
        state_path: Path | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        zone: Callable[[], ZoneInfo] | None = None,
    ) -> None:
        self._path = path
        self._state_path = state_path
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._zone = zone or user_zone
        # Re-entrant: a public method that writes calls the readers under it.
        self._lock = threading.RLock()

    # Resolved per call, for the ideas.ResponseStore reason: tests swap
    # `settings`, and a path captured at import would keep the old one.
    @property
    def path(self) -> Path:
        return self._path or settings.todos_path

    @property
    def state_path(self) -> Path:
        return self._state_path or settings.todo_reminders_path

    def _now(self) -> datetime:
        return self._clock()

    def zone(self) -> ZoneInfo:
        """The zone this store reads a bare local time in."""
        return self._zone()

    # --- files -------------------------------------------------------------

    def _read(self) -> dict[str, Any]:
        """The item file, or a fresh one if absent. Raises TodoError if it is
        unreadable: unlike the ideas responses, this file IS the list, and
        starting over on the next write would erase it."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"next_id": 1, "items": []}
        except (OSError, ValueError) as e:
            logger.error("to-do list unreadable at %s: %s", self.path, e)
            raise TodoError("the to-do list file is unreadable, so nothing was changed") from e
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("items"), list)
            or not isinstance(raw.get("next_id"), int)
        ):
            logger.error("to-do list at %s has the wrong shape", self.path)
            raise TodoError("the to-do list file is unreadable, so nothing was changed")
        return raw

    def _read_state(self) -> dict[str, dict[str, Any]]:
        """Reminder bookkeeping. Unreadable reads as none: the cost is a
        reminder firing again, never the list."""
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            logger.warning("ignoring unreadable to-do reminder state: %s", e)
            return {}
        items = raw.get("items") if isinstance(raw, dict) else None
        return items if isinstance(items, dict) else {}

    @staticmethod
    def _atomic_write(path: Path, data: dict[str, Any], *, private: bool) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700 if private else 0o755)
        # Atomic replace: the model and the phone read this file, and a
        # half-written one would read as unreadable.
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            if not private:
                os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _write(self, data: dict[str, Any]) -> None:
        self._prune(data)
        self._atomic_write(self.path, data, private=False)

    def _write_state(self, state: dict[str, dict[str, Any]]) -> None:
        self._atomic_write(self.state_path, {"items": state}, private=True)

    def _prune(self, data: dict[str, Any]) -> None:
        now = self._now()
        keep = []
        for item in data["items"]:
            closed = _parse_instant(item.get("closed") or "")
            if closed is not None:
                days = KEEP_DONE_DAYS if item["state"] == "done" else KEEP_DROPPED_DAYS
                if now - closed > timedelta(days=days):
                    continue
            keep.append(item)
        data["items"] = keep

    # --- parsing -----------------------------------------------------------

    def _parse_moment(self, value: Any, what: str) -> tuple[str, datetime | None]:
        """(canonical text, instant or None for a bare date)."""
        if not isinstance(value, str):
            raise TodoError(f"{what} must be {DUE_FORMAT_HELP}")
        value = value.strip()
        if _DATE_RE.fullmatch(value):
            try:
                date.fromisoformat(value)
            except ValueError:
                raise TodoError(f"{what} {value!r} is not a real date; use {DUE_FORMAT_HELP}") from None
            return value, None
        local = _LOCAL_TIME_RE.fullmatch(value)
        try:
            if local:
                moment = datetime.fromisoformat(f"{local[1]}T{local[2]}").replace(tzinfo=self._zone())
            else:
                moment = datetime.fromisoformat(value)
                if moment.tzinfo is None:
                    raise ValueError
        except ValueError:
            raise TodoError(f"{what} {value!r} isn't readable; use {DUE_FORMAT_HELP}") from None
        moment = moment.replace(second=0, microsecond=0)
        if moment < self._now() - _PAST_GRACE:
            raise TodoError(f"{what} {value!r} has already passed; give a later time, or a date")
        # Pinned: the offset it has now is kept, so the list doesn't move if
        # the phone later reports another zone.
        return _iso(moment.astimezone(moment.tzinfo)), moment

    def _parse_due(self, value: Any) -> str | None:
        if value is None:
            return None
        return self._parse_moment(value, "due")[0]

    # --- lookups -----------------------------------------------------------

    @staticmethod
    def _find(data: dict[str, Any], item_id: Any) -> dict[str, Any]:
        if not isinstance(item_id, str) or not _ID_RE.fullmatch(item_id):
            raise TodoError(f"{item_id!r} is not a to-do id (they look like t12)")
        for item in data["items"]:
            if item["id"] == item_id:
                return item
        raise TodoError(f"there is no to-do {item_id} (it may have aged off the list)")

    def _with_state(self, item: dict[str, Any], state: dict[str, dict[str, Any]]) -> dict[str, Any]:
        out = dict(item)
        snoozed = state.get(item["id"], {}).get("snoozed_until")
        out["snoozed_until"] = snoozed if item["state"] == "open" else None
        return out

    def items(self) -> list[dict[str, Any]]:
        """Every item, in list order. An unreadable file reads as empty here
        (and is logged); only writes refuse it."""
        with self._lock:
            try:
                data = self._read()
            except TodoError:
                return []
            state = self._read_state()
            return [self._with_state(i, state) for i in data["items"]]

    def get(self, item_id: str) -> dict[str, Any]:
        with self._lock:
            return self._with_state(self._find(self._read(), item_id), self._read_state())

    def client_items(self) -> list[dict[str, Any]]:
        """What the phone lists: open and proposed, plus done this week.
        Dropped is the store's memory, not the user's list."""
        cutoff = self._now() - timedelta(days=RECENT_DONE_DAYS)
        out = []
        for item in self.items():
            if item["state"] in ACTIVE:
                out.append(item)
            elif item["state"] == "done":
                closed = _parse_instant(item.get("closed") or "")
                if closed is not None and closed >= cutoff:
                    out.append(item)
        return out

    # --- writes ------------------------------------------------------------

    def add(
        self, title: Any, *, notes: Any = None, due: Any = None, source: Any
    ) -> dict[str, Any]:
        if source not in SOURCES:
            raise TodoError("source must be 'user' (they asked for it) or 'inferred' (you noticed it)")
        title = _clean_title(title)
        notes = _clean_notes(notes)
        with self._lock:
            data = self._read()
            due_text = self._parse_due(due)
            active = [i for i in data["items"] if i["state"] in ACTIVE]
            for item in data["items"]:
                if not _same_title(item["title"], title):
                    continue
                if item["state"] in ACTIVE:
                    raise TodoError(f"that is already on the list as {item['id']} ({item['state']})")
                if item["state"] == "dropped" and source == "inferred":
                    raise TodoError(
                        f"the user dropped this before ({item['id']}); don't propose it again"
                    )
            if len(active) >= MAX_ACTIVE:
                raise TodoError(
                    f"the list is full ({MAX_ACTIVE} open or proposed); close or drop some first"
                )
            now = _iso(self._now())
            item = {
                "id": f"t{data['next_id']}",
                "title": title,
                "notes": notes,
                "due": due_text,
                "state": "open" if source == "user" else "proposed",
                "source": source,
                "created": now,
                "updated": now,
                "closed": None,
            }
            data["next_id"] += 1
            data["items"].append(item)
            self._write(data)
            return self._with_state(item, self._read_state())

    def update(
        self, item_id: Any, *, title: Any = _UNSET, notes: Any = _UNSET, due: Any = _UNSET
    ) -> dict[str, Any]:
        with self._lock:
            data = self._read()
            item = self._find(data, item_id)
            if item["state"] not in ACTIVE:
                raise TodoError(f"{item['id']} is {item['state']}; add a new to-do instead")
            if title is not _UNSET:
                item["title"] = _clean_title(title)
            if notes is not _UNSET:
                item["notes"] = _clean_notes(notes)
            due_moved = False
            if due is not _UNSET:
                new_due = self._parse_due(due)
                due_moved = new_due != item["due"]
                item["due"] = new_due
            item["updated"] = _iso(self._now())
            self._write(data)
            state = self._read_state()
            if due_moved and item["id"] in state:
                # A new time is a new reminder, not a re-nudge of the old one.
                del state[item["id"]]
                self._write_state(state)
            return self._with_state(item, state)

    def _close(self, item_id: Any, to: str) -> dict[str, Any]:
        with self._lock:
            data = self._read()
            item = self._find(data, item_id)
            if item["state"] == to:
                return self._with_state(item, self._read_state())
            if item["state"] not in ACTIVE:
                raise TodoError(f"{item['id']} is already {item['state']}")
            now = _iso(self._now())
            item.update(state=to, closed=now, updated=now)
            self._write(data)
            return self._with_state(item, self._read_state())

    def done(self, item_id: Any) -> dict[str, Any]:
        return self._close(item_id, "done")

    def drop(self, item_id: Any) -> dict[str, Any]:
        return self._close(item_id, "dropped")

    def accept(self, item_id: Any) -> dict[str, Any]:
        with self._lock:
            data = self._read()
            item = self._find(data, item_id)
            if item["state"] != "proposed":
                raise TodoError(f"{item['id']} is not a proposal (it is {item['state']})")
            item.update(state="open", updated=_iso(self._now()))
            self._write(data)
            return self._with_state(item, self._read_state())

    def snooze(self, item_id: Any, until: Any) -> dict[str, Any]:
        with self._lock:
            data = self._read()
            item = self._find(data, item_id)
            if item["state"] != "open":
                raise TodoError(f"only an open to-do can be snoozed; {item['id']} is {item['state']}")
            text, moment = self._parse_moment(until, "until")
            if moment is None:
                raise TodoError(f"until needs a time, not just a day: YYYY-MM-DD HH:MM")
            if moment - self._now() > MAX_SNOOZE:
                raise TodoError("a snooze can be at most a year; set a due date instead")
            state = self._read_state()
            entry = state.setdefault(item["id"], {})
            entry["snoozed_until"] = text
            self._write_state(state)
            return self._with_state(item, state)

    # --- reminders ---------------------------------------------------------

    def due_for_reminder(self, *, renudge: bool) -> list[dict[str, Any]]:
        """Open items whose reminder is due now, in list order.

        Only an item with a due TIME pushes on its own; a dated or undated
        one is on the screen and in the brief. A snooze, on any open item, is
        a time to push at. Re-nudge (on by default, per person) fires a
        reminder that went unanswered again a day after it last fired."""
        now = self._now()
        out = []
        with self._lock:
            state = self._read_state()
            for item in self.items():
                if item["state"] != "open":
                    continue
                entry = state.get(item["id"], {})
                if entry.get("pending"):
                    continue
                snoozed = _parse_instant(entry.get("snoozed_until") or "")
                fired = _parse_instant(entry.get("fired_at") or "")
                if snoozed is not None:
                    at = snoozed
                elif fired is None:
                    at = due_instant(item["due"])
                elif renudge:
                    at = fired + RENUDGE_AFTER
                else:
                    at = None
                if at is not None and now >= at:
                    out.append(item)
        return out

    def mark_fired(self, item_ids: Iterable[str]) -> dict[str, str]:
        """Record that these items' reminder turn is queued. Returns the token
        `settle` takes: id -> the fired_at written now."""
        now = _iso(self._now())
        fired: dict[str, str] = {}
        with self._lock:
            state = self._read_state()
            for item_id in item_ids:
                entry = state.setdefault(item_id, {})
                entry.update(fired_at=now, pending=True)
                entry.pop("snoozed_until", None)
                fired[item_id] = now
            self._write_state(state)
        return fired

    def settle(self, fired: dict[str, str], *, delivered: bool) -> list[dict[str, Any]]:
        """The reminder turn ended. Returns the items the server must still
        send as a plain notice: when the turn did not deliver (it errored, or
        replied nothing or `[silent]`), every item it did not deal with.

        Dealt with means a state change on the list during the turn -- done,
        dropped, snoozed or moved -- never an unseen decision to stay quiet
        (Mike, 2026-11-04: no silent skip)."""
        with self._lock:
            state = self._read_state()
            try:
                current = {i["id"]: i for i in self._read()["items"]}
            except TodoError:
                current = {}
            fallback = []
            for item_id, fired_at in fired.items():
                entry = state.get(item_id)
                if entry is None or entry.get("fired_at") != fired_at:
                    continue  # the due time moved: the bookkeeping started over
                entry.pop("pending", None)
                item = current.get(item_id)
                handled = (
                    item is None
                    or item["state"] != "open"
                    or entry.get("snoozed_until") is not None
                )
                if not delivered and not handled:
                    fallback.append(self._with_state(item, state))
            self._write_state(state)
            return fallback

    def recover(self) -> None:
        """At startup: a reminder whose turn was queued when the server went
        down never ran. Fire it again at once, rather than a day later."""
        with self._lock:
            state = self._read_state()
            lost = [k for k, v in state.items() if v.get("pending")]
            if not lost:
                return
            now = _iso(self._now())
            for item_id in lost:
                state[item_id].pop("pending", None)
                state[item_id]["snoozed_until"] = now
            self._write_state(state)
            logger.info("re-queuing %d to-do reminder(s) lost to a restart", len(lost))

    # --- proposals ---------------------------------------------------------

    def take_stale_proposals(self) -> list[dict[str, Any]]:
        """Proposals unconfirmed for two days that haven't been raised again,
        marked as raised. Called by the turn context, so each is raised once
        more and then only sits on the list (Mike, 2026-11-04)."""
        now = self._now()
        with self._lock:
            state = self._read_state()
            out = []
            for item in self.items():
                if item["state"] != "proposed" or state.get(item["id"], {}).get("reasked_at"):
                    continue
                created = _parse_instant(item["created"])
                if created is not None and now - created >= REASK_AFTER:
                    out.append(item)
                    state.setdefault(item["id"], {})["reasked_at"] = _iso(now)
            if out:
                self._write_state(state)
            return out


todo_store = TodoStore()
