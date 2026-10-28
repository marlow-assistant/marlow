"""What the app may show of Marlow's identity and schedules
(docs/tasks/ux-lane.md §3a).

Two read-only views: an index of the files Marlow lives by plus its schedule
store, and the text of one indexed file.

**The security boundary is the enumeration.** `list_files()` builds the only
set of files that can ever be read; `read_file()` looks the requested id up in
that set and opens the path the enumeration produced. The id is a key, never a
path -- it is not joined onto anything, so `../`, absolute paths and
`memory/sub/x.md` are simply not in the set. On top of that, every candidate
is dropped if it is a symlink (not followed: a link out of `memory/` would
otherwise read anything the server can), if it matches `is_secret_path` or
`AGENT_CONFIG_PATHS`, or if it is not a regular file. A file that does not
exist and a file that is not allowed are the same answer (None -> 404), so the
route cannot be used to probe which is which.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from server import schedules
from server.boundaries import is_agent_config_path, is_secret_path
from server.config import settings
from server.identity import _DESCRIPTION_SCAN_BYTES, _decode_prefix, _extract_description
from server.log import get_logger

logger = get_logger(__name__)

# Largest file today is 42 KB (§3a); this is a ceiling, not a target.
MAX_FILE_BYTES = 256 * 1024

# Every day/month pattern `_cron_values` accepts recurs within a year, except
# a Feb 29-only expression, which can be up to four years out; that one gets
# None rather than a scan of 1461 days.
_LOOKAHEAD_DAYS = 366


@dataclass(frozen=True)
class IndexedFile:
    id: str
    kind: str
    path: Path
    modified_at: float
    size: int


# --- enumeration ---------------------------------------------------------------

def _file_id(path: Path, fallback: str) -> str:
    """Path relative to the working dir, as a posix string. Outside it, the
    fallback -- ids are opaque keys the client hands back, not paths it
    resolves, so all that matters is that they are stable and unique."""
    try:
        return path.relative_to(settings.working_dir).as_posix()
    except ValueError:
        return fallback


def _candidate(path: Path, file_id: str, kind: str) -> IndexedFile | None:
    """One file, or None if it may not be shown. lstat, so a symlink is seen as
    a symlink instead of as whatever it points at."""
    try:
        st = path.lstat()
    except OSError:
        return None  # missing (or unreadable) is omitted, never a 500
    if not stat.S_ISREG(st.st_mode):  # also rejects symlinks, dirs, sockets
        return None
    if is_secret_path(str(path)) or is_agent_config_path(str(path)):
        return None
    return IndexedFile(file_id, kind, path, st.st_mtime, st.st_size)


def _read_prefix(path: Path, limit: int) -> tuple[bytes, os.stat_result] | None:
    """Up to `limit` bytes, refusing a symlink at open time (O_NOFOLLOW). The
    enumeration can be a moment stale, and this is the last line before the
    disk. Returns the fstat of the opened file so mtime matches the bytes."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        with os.fdopen(fd, "rb", closefd=False) as fh:
            return fh.read(limit), st
    except OSError:
        return None
    finally:
        os.close(fd)


def _job_names() -> list[str]:
    """Jobs named by a VALID store, deduped, in store order."""
    path = settings.schedules_path
    if schedules.rejection_reason(path) is not None:
        return []
    store = schedules.load(path)
    if store is None:
        return []
    return list(dict.fromkeys(e.job for e in store.entries if e.job))


def list_files() -> list[IndexedFile]:
    """Every file the app may show, in display order: soul, user, instructions,
    memory by filename, jobs by id."""
    found: list[IndexedFile | None] = [
        _candidate(settings.soul_path, _file_id(settings.soul_path, settings.soul_path.name), "soul"),
        _candidate(settings.user_path, _file_id(settings.user_path, settings.user_path.name), "user"),
    ]
    claude_md = settings.working_dir / "CLAUDE.md"
    found.append(_candidate(claude_md, "CLAUDE.md", "instructions"))

    # Top-level regular *.md only. Subfolders are not injected into the prompt,
    # so they are not "what Marlow reads every turn" (§3a, parked).
    try:
        names = sorted(n for n in os.listdir(settings.memory_dir) if n.endswith(".md"))
    except OSError:
        names = []
    for name in names:
        path = settings.memory_dir / name
        found.append(_candidate(path, _file_id(path, f"memory/{name}"), "memory"))

    cron_dir = settings.schedules_path.parent
    for job in sorted(_job_names()):
        path = cron_dir / f"{job}.md"
        found.append(_candidate(path, _file_id(path, f"cron/{job}.md"), "job"))

    files: list[IndexedFile] = []
    seen: set[str] = set()
    for f in found:
        if f is not None and f.id not in seen:
            seen.add(f.id)
            files.append(f)
    return files


def _description(f: IndexedFile) -> str | None:
    if f.kind != "memory":
        return None
    read = _read_prefix(f.path, _DESCRIPTION_SCAN_BYTES)
    if read is None:
        return None
    raw = read[0]
    try:
        text = _decode_prefix(raw)
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    return _extract_description(text) or None


def _serialize_file(f: IndexedFile) -> dict[str, Any]:
    return {
        "id": f.id,
        "kind": f.kind,
        "modified_at": f.modified_at,
        "size": f.size,
        "description": _description(f),
    }


def read_file(file_id: str) -> dict[str, Any] | None:
    """The text of an indexed file, or None for anything not in the index.

    `file_id` is only compared against ids the enumeration produced; the path
    that gets opened is the enumeration's, never one derived from the string."""
    match = next((f for f in list_files() if f.id == file_id), None)
    if match is None:
        return None
    read = _read_prefix(match.path, MAX_FILE_BYTES + 1)
    if read is None:
        return None
    raw, st = read
    truncated = len(raw) > MAX_FILE_BYTES
    if truncated:
        raw = raw[:MAX_FILE_BYTES]
        try:
            text = _decode_prefix(raw)  # the cut may split a codepoint
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
    else:
        text = raw.decode("utf-8", errors="replace")
    return {
        "id": match.id,
        "kind": match.kind,
        "modified_at": st.st_mtime,
        "text": text,
        "truncated": truncated,
    }


# --- recurrence and next fire -------------------------------------------------

def _weekdays(field: str) -> set[int]:
    """Days of week as 0-6 with Sunday 0: cron accepts 7 as a second Sunday."""
    return {v % 7 for v in schedules._cron_values(field, 0, 7)}


def recurrence(entry: schedules.ScheduleEntry) -> dict[str, Any]:
    """What an entry's schedule IS, as structure; never how it reads.

    Wording and grouping are the app's (Mike, 2026-09-29: a UX tweak must not
    need a box deploy). This stays here because the server owns the cron
    parser, and the day-of-month/day-of-week rules are the part of cron a
    client would most likely get wrong.

    `type` is literal about the fields: `daily` needs day-of-month, month and
    day-of-week all `*`; `weekly` restricts only day-of-week; `monthly` only
    day-of-month. Anything else -- a month restriction, both day fields, a
    stepped day field like `*/2` -- is `other`, and the app shows the raw
    `cron`. `times` are every "HH:MM" the entry fires at, sorted; weekdays
    are 0-6, Sunday 0.
    """
    if entry.at is not None:
        return {"type": "once"}
    fields = (entry.cron or "").split()
    if len(fields) != 5:
        return {"type": "other"}
    minute, hour, dom, month, dow = fields
    times = sorted(
        f"{h:02d}:{m:02d}"
        for h in schedules._cron_values(hour, 0, 23)
        for m in schedules._cron_values(minute, 0, 59)
    )
    if month != "*":
        return {"type": "other"}
    if dom == "*" and dow == "*":
        return {"type": "daily", "times": times}
    if dom == "*" and not dow.startswith("*"):
        return {"type": "weekly", "weekdays": sorted(_weekdays(dow)), "times": times}
    if dow == "*" and not dom.startswith("*"):
        return {"type": "monthly", "days": sorted(schedules._cron_values(dom, 1, 31)), "times": times}
    return {"type": "other"}


def _wall_exists(local: datetime) -> bool:
    """False for a wall time inside a spring-forward gap: converting to UTC and
    back lands on a different wall time. Skipped, not guessed -- cron itself
    behaves differently across implementations there."""
    back = local.astimezone(timezone.utc).astimezone(local.tzinfo)
    return back.replace(tzinfo=None) == local.replace(tzinfo=None)


def next_fire_at(
    entry: schedules.ScheduleEntry, timezone_name: str, now: datetime
) -> float | None:
    """Epoch seconds of the entry's next firing after `now`, None if there is
    none within a year. `now` is a parameter so tests need no clock.

    An `at` in the past is returned as-is: the app shows it as overdue, which
    is the state worth seeing (a one-off that has not fired yet)."""
    if entry.at is not None:
        return schedules.at_instant(entry.at, timezone_name).timestamp()
    if entry.cron is None:
        return None

    minute_f, hour_f, dom_f, month_f, dow_f = entry.cron.split()
    minutes = sorted(schedules._cron_values(minute_f, 0, 59))
    hours = sorted(schedules._cron_values(hour_f, 0, 23))
    doms = schedules._cron_values(dom_f, 1, 31)
    months = schedules._cron_values(month_f, 1, 12)
    dows = _weekdays(dow_f)
    # Vixie cron: a field starting with `*` counts as unrestricted, so `*/2`
    # in day-of-month still ANDs with day-of-week rather than OR-ing.
    dom_free, dow_free = dom_f.startswith("*"), dow_f.startswith("*")

    zone = ZoneInfo(timezone_name)
    today = now.astimezone(zone).date()
    for offset in range(_LOOKAHEAD_DAYS + 1):
        day: date = today + timedelta(days=offset)
        if day.month not in months:
            continue
        dom_hit = day.day in doms
        dow_hit = day.isoweekday() % 7 in dows
        if dom_free or dow_free:
            hit = dom_hit and dow_hit
        else:
            hit = dom_hit or dow_hit  # both restricted: either suffices
        if not hit:
            continue
        for hour in hours:
            for minute in minutes:
                local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
                if _wall_exists(local) and local > now:
                    return local.timestamp()
    return None


def schedules_view(now: datetime) -> dict[str, Any]:
    """The schedule store as the Schedule panel shows it.

    An invalid store is reported, not hidden: `sync_after_turn` keeps the last
    good crontab running, so it is the state nobody would otherwise see
    (§3a). `rejection_reason` is asked first so the ERROR `load` logs is not
    repeated on every poll of the sheet."""
    path = settings.schedules_path
    if not path.exists():
        return {"timezone": None, "rejected": None, "entries": []}
    reason = schedules.rejection_reason(path)
    store = schedules.load(path) if reason is None else None
    if store is None:
        return {
            "timezone": None,
            "rejected": reason if reason is not None else "schedule store could not be read",
            "entries": [],
        }
    job_files = {f.id: f.id for f in list_files() if f.kind == "job"}
    cron_dir = path.parent
    entries = []
    for e in store.entries:
        job_file = None
        if e.job:
            job_path = cron_dir / f"{e.job}.md"
            job_file = job_files.get(_file_id(job_path, f"cron/{e.job}.md"))
        entries.append({
            "id": e.id,
            "cron": e.cron,
            "at": e.at,
            "recurrence": recurrence(e),
            "next_fire_at": next_fire_at(e, store.timezone, now),
            "notify": e.notify,
            "prompt": e.prompt,
            "job": e.job,
            "job_file": job_file,
        })
    return {"timezone": store.timezone, "rejected": None, "entries": entries}


def identity_index(now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    return {
        "files": [_serialize_file(f) for f in list_files()],
        "schedules": schedules_view(now),
    }
