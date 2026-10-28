"""Validate and install Marlow's declarative schedules.

The server process, not the model, owns the crontab. ``schedules.json`` is
model-written input consumed outside the invocation sandbox, so this module's
allowlist-shaped validator is the security boundary: no executable text comes
from the store, and one invalid entry aborts the entire render.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from server.config import settings
from server.log import get_logger
from server.preferences import EFFORTS, PreferencesStore, minutes, preferences_store
from server.turn_context import is_valid_timezone

logger = get_logger(__name__)

MAX_ENTRIES = 30
MIN_FIRE_INTERVAL_MINUTES = 15

# Store-wide budget on turns per day, checked across ALL entries. The
# per-entry floor above bounds one runaway expression; it says nothing about
# thirty legal ones, and 30 x 96 = 2880 turns a day is the actual worst case it
# leaves open (/code-review medium, 2026-08-30). Real use is ~3 a day, so 96 --
# one entry sitting at the 15-minute floor around the clock -- is 30x headroom
# and still cuts the worst case by 30x.
#
# NOT a store-wide minimum GAP, which was the obvious first move: it would
# reject Mike's own live schedule, where `morning` at 07:30 and
# `alberta-weekly` at 07:33 legitimately fire three minutes apart on Mondays.
# Two turns close together are fine; it is the daily volume that costs.
#
# `at` entries are deliberately outside this budget. Each fires exactly once
# and then prunes itself, so MAX_ENTRIES already bounds them at thirty turns
# for the lifetime of the store -- there is no rate to cap. Thirty one-offs at
# the same minute would queue thirty turns at once, which the work queue
# serializes and drains in ~10 minutes; accepted rather than machined around.
MAX_DAILY_FIRINGS = 96

# The harness's own upkeep (docs/tasks/default-infrastructure.md, tier 1), as
# minutes after the end of "Your day". Not store entries: the renderer adds
# these lines itself, so the user can't delete them by asking Marlow to "stop
# the 1am message", and they sit outside MAX_ENTRIES and MAX_DAILY_FIRINGS,
# which bound what the model can schedule. `ideas` runs first because it reads
# the day's conversation, which `consolidate` then retires; that ordering used
# to be a sentence in the identity and is now these two numbers. The defaults
# (day end 23:00) give the 00:30 and 01:00 every box ran before.
PLUMBING_OFFSETS: tuple[tuple[str, int], ...] = (("ideas", 90), ("consolidate", 120))
PLUMBING_JOBS: frozenset[str] = frozenset(job for job, _ in PLUMBING_OFFSETS)

# The check-in (tier 2): a built-in feature, not plumbing -- its reply reaches
# the phone and the user can turn it off -- but timed by the harness the same
# way, from "Your day" and `checkin_every_hours`. "email-flash" is its name in
# code and on disk; the user only ever hears "check-in".
CHECKIN_JOB = "email-flash"
# The last check-in is at least this long before the day ends: a check-in at
# 22:30 for a day that ends at 23:00 is a message about tomorrow's inbox.
CHECKIN_MIN_BEFORE_END_MINUTES = 120
# Every job the renderer adds itself. None may be a store entry, or the
# harness and the store would both fire it.
HARNESS_JOBS: frozenset[str] = PLUMBING_JOBS | {CHECKIN_JOB}
def _command() -> str:
    # The configured path is validated in config.py (a plain absolute path),
    # so it carries nothing that crontab or sh would read as syntax.
    return f"/usr/bin/python3 {settings.cron_entrypoint}"
_ID_RE = re.compile(r"[a-z0-9-]{1,40}\Z")
_CRON_FIELD_RE = re.compile(r"[0-9*,/-]+\Z")
# Two accepted `at` spellings. The model writes the floating one ("22:14"),
# which is what a person says; the harness pins it to an instant on the next
# sync and writes the offset form back. Both are strict round-trips -- a
# `-02:30` with a colon is rejected rather than normalised, because the
# validator is the security boundary and only the harness writes the pinned
# form.
_AT_FLOATING_FORMAT = "%Y-%m-%dT%H:%M"
_AT_PINNED_FORMAT = "%Y-%m-%dT%H:%M%z"
_INSTALL_TIMEOUT_SECONDS = 30

# The renderer owns the complete crontab. Variable names and their settings
# sources are protected constants; values come from the server's administrator-
# supplied environment, never from schedules.json. That keeps cron and the
# already-running server pointed at the same working/data/log directories when
# a deployment uses the supported MARLOW_* overrides.
_CRONTAB_ENV_SETTINGS = (
    ("MARLOW_WORKING_DIR", "working_dir"),
    ("MARLOW_DATA_DIR", "data_dir"),
    ("MARLOW_LOG_DIR", "log_dir"),
    ("MARLOW_CLAUDE_BIN", "claude_bin"),
    ("MARLOW_SERVER_REPO", "server_repo"),
    ("MARLOW_INTERNAL_SOCKET", "internal_socket_path"),
    # The entrypoint re-renders this crontab after a one-shot fires, with only
    # these variables: without it, a second instance's re-render would point
    # its jobs back at the first instance's copy.
    ("MARLOW_CRON_ENTRYPOINT", "cron_entrypoint"),
)

SCHEDULES_PATH = settings.schedules_path


@dataclass(frozen=True)
class ScheduleEntry:
    id: str
    at: str | None = None
    cron: str | None = None
    notify: str | None = None
    prompt: str | None = None
    job: str | None = None
    # Thinking effort for a `job` or `prompt` entry's turn, chosen by Marlow
    # when it writes the entry. The server reads it at enqueue (app.py
    # `_cron_effort`, which also owns which product jobs it may not change);
    # None means the job's code default or `settings.cron_effort`.
    effort: str | None = None
    # No `payload`/`payload_kind` accessor here on purpose. The obvious one
    # (`self.notify or self.prompt or self.job`) COLLAPSES the three kinds this
    # module works to keep apart, and that collapse is precisely the bug fixed
    # on this branch: an entry id reaching the field that selects job
    # behaviour. Callers dispatch on the specific field they mean.


@dataclass(frozen=True)
class Schedules:
    timezone: str
    entries: tuple[ScheduleEntry, ...]

    def entry(self, entry_id: str) -> ScheduleEntry | None:
        return next((entry for entry in self.entries if entry.id == entry_id), None)


class ScheduleValidationError(ValueError):
    pass


def _crontab_preamble() -> list[str]:
    lines = ['MAILTO=""']
    for variable, setting_name in _CRONTAB_ENV_SETTINGS:
        value = str(getattr(settings, setting_name))
        # Environment values are administrator-controlled, not model input, but
        # a newline would still create a second crontab line. Reject it instead
        # of weakening the renderer's invariant that executable lines are made
        # entirely from protected constants.
        if any(character in value for character in ("\n", "\r", "\0")):
            raise ScheduleValidationError(f"invalid newline in {variable}")
        lines.append(f"{variable}={value}")
    return lines


def _cron_values(field: str, minimum: int, maximum: int) -> set[int]:
    if not _CRON_FIELD_RE.fullmatch(field):
        raise ScheduleValidationError(f"invalid cron field {field!r}")

    values: set[int] = set()
    for item in field.split(","):
        if not item:
            raise ScheduleValidationError(f"invalid cron field {field!r}")
        if item.count("/") > 1:
            raise ScheduleValidationError(f"invalid cron step {item!r}")
        base, separator, step_text = item.partition("/")
        step = 1
        if separator:
            if not step_text.isdigit() or int(step_text) <= 0:
                raise ScheduleValidationError(f"invalid cron step {item!r}")
            step = int(step_text)

        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            if base.count("-") != 1:
                raise ScheduleValidationError(f"invalid cron range {item!r}")
            start_text, end_text = base.split("-", 1)
            if not start_text.isdigit() or not end_text.isdigit():
                raise ScheduleValidationError(f"invalid cron range {item!r}")
            start, end = int(start_text), int(end_text)
        elif base.isdigit():
            start = int(base)
            end = maximum if separator else start
        else:
            raise ScheduleValidationError(f"invalid cron item {item!r}")

        if start < minimum or end > maximum or start > end:
            raise ScheduleValidationError(f"cron value out of range in {item!r}")
        values.update(range(start, end + 1, step))

    if not values:
        raise ScheduleValidationError(f"empty cron field {field!r}")
    return values


def _validate_cron(expression: str) -> None:
    fields = expression.split()
    if len(fields) != 5:
        raise ScheduleValidationError("cron must contain exactly five fields")
    minute, hour, day, month, weekday = fields
    minutes = _cron_values(minute, 0, 59)
    hours = _cron_values(hour, 0, 23)
    _cron_values(day, 1, 31)
    _cron_values(month, 1, 12)
    _cron_values(weekday, 0, 7)

    # Day/month restrictions can only remove firing days. The closest two
    # possible firings therefore come from the expression's times of day;
    # checking that cycle is conservative and avoids building a scheduler just
    # to validate the frequency floor.
    times = sorted(hour_value * 60 + minute_value for hour_value in hours for minute_value in minutes)
    gaps = [later - earlier for earlier, later in zip(times, times[1:])]
    gaps.append(times[0] + 24 * 60 - times[-1])
    if min(gaps) < MIN_FIRE_INTERVAL_MINUTES:
        raise ScheduleValidationError(
            f"cron fires more often than every {MIN_FIRE_INTERVAL_MINUTES} minutes"
        )


def _cron_firings_per_day(expression: str) -> int:
    """Upper bound on turns per day for one ALREADY-VALIDATED cron expression.

    Counts every calendar day as a firing day on purpose. The day, month and
    weekday fields can only REMOVE days, so ignoring them overcounts, which is
    the safe direction for a budget: `33 7 * * 1` scores 1/day rather than 1/7.
    That keeps this a field count rather than a scheduler, and MAX_DAILY_FIRINGS
    has the headroom to absorb the overcount.
    """
    minute, hour, _day, _month, _weekday = expression.split()
    return len(_cron_values(minute, 0, 59)) * len(_cron_values(hour, 0, 23))


def at_instant(value: str, timezone_name: str) -> datetime:
    """The moment an already-validated ``at`` denotes.

    A pinned value carries its own offset and ignores the store's zone; that is
    the whole point of pinning (defect 2, 2026-08-30: editing the store's
    `timezone` field silently moved a pending reminder three and a half hours,
    and it fired at 1:44am). A floating value still resolves against the store,
    which is correct for the window between the model writing it and the next
    sync pinning it.
    """
    try:
        return datetime.strptime(value, _AT_PINNED_FORMAT)
    except ValueError:
        return datetime.strptime(value, _AT_FLOATING_FORMAT).replace(
            tzinfo=ZoneInfo(timezone_name)
        )


def _validate_at(value: str) -> None:
    for fmt in (_AT_FLOATING_FORMAT, _AT_PINNED_FORMAT):
        try:
            parsed = datetime.strptime(value, fmt)
        except (TypeError, ValueError):
            continue
        if parsed.strftime(fmt) == value:
            return
    raise ScheduleValidationError(
        "at must be a strict ISO local minute, with or without a +HHMM offset"
    )


def _require_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScheduleValidationError(f"{name} must be a non-empty string")
    return value


def _parse_entry(raw: Any, cron_dir: Path) -> ScheduleEntry:
    if not isinstance(raw, dict):
        raise ScheduleValidationError("each entry must be an object")

    allowed = {"id", "at", "cron", "notify", "prompt", "job", "effort"}
    unknown = set(raw) - allowed
    if unknown:
        raise ScheduleValidationError(f"entry contains unsupported keys: {sorted(unknown)!r}")

    entry_id = _require_string(raw.get("id"), "entry id")
    if not _ID_RE.fullmatch(entry_id):
        raise ScheduleValidationError(f"invalid entry id {entry_id!r}")

    schedule_keys = [key for key in ("at", "cron") if key in raw]
    payload_keys = [key for key in ("notify", "prompt", "job") if key in raw]
    if len(schedule_keys) != 1:
        raise ScheduleValidationError(f"entry {entry_id!r} must contain exactly one of at or cron")
    if len(payload_keys) != 1:
        raise ScheduleValidationError(
            f"entry {entry_id!r} must contain exactly one of notify, prompt, or job"
        )

    schedule_key = schedule_keys[0]
    schedule_value = _require_string(raw[schedule_key], schedule_key)
    if schedule_key == "cron":
        _validate_cron(schedule_value)
    else:
        _validate_at(schedule_value)

    payload_key = payload_keys[0]
    payload_value = _require_string(raw[payload_key], payload_key)
    if payload_key == "job":
        if not _ID_RE.fullmatch(payload_value):
            raise ScheduleValidationError(f"invalid job name {payload_value!r}")
        if payload_value in HARNESS_JOBS:
            # Otherwise the harness and the store could both fire it. Worded
            # for the model, which reads this through the turn context.
            raise ScheduleValidationError(
                f"entry {entry_id!r}: the {payload_value} job runs automatically, timed from "
                "\"Your day\" in the app's Settings; remove this entry"
            )
        prompt_path = cron_dir / f"{payload_value}.md"
        if not prompt_path.is_file():
            raise ScheduleValidationError(f"job prompt does not exist: {prompt_path}")

    effort = raw.get("effort")
    if "effort" in raw:
        # A reminder never reaches the model, so an effort on one is a
        # misunderstanding worth refusing rather than silently ignoring.
        if payload_key == "notify":
            raise ScheduleValidationError(f"entry {entry_id!r}: effort does not apply to notify")
        if effort not in EFFORTS:
            raise ScheduleValidationError(
                f"entry {entry_id!r}: effort must be one of {', '.join(EFFORTS)}"
            )

    values: dict[str, str | None] = {
        "at": None,
        "cron": None,
        "notify": None,
        "prompt": None,
        "job": None,
    }
    values[schedule_key] = schedule_value
    values[payload_key] = payload_value
    return ScheduleEntry(id=entry_id, effort=effort, **values)


def load(path: str | os.PathLike[str]) -> Schedules | None:
    """Parse and fully validate a schedule store. Never raises.

    A store that does not exist is the ordinary state of a box with no
    schedules, so it returns None QUIETLY. Only a store that exists and is
    wrong is logged, and it is logged at ERROR — the whole never-partial
    contract rests on that line being rare enough to notice, and an
    every-turn "rejected: No such file" would bury it (review 2026-08-30).
    """
    schedule_path = Path(path)
    if not schedule_path.exists():
        return None
    try:
        return _parse_store(json.loads(schedule_path.read_text()), schedule_path)
    except Exception as exc:
        logger.error("schedule store rejected at %s: %s", schedule_path, exc)
        return None


def _parse_store(raw: Any, schedule_path: Path) -> Schedules:
    """The validation itself, raising rather than logging.

    Split out of `load` so `rejection_reason` can ask "would this be refused,
    and why" without either duplicating the rules or emitting a second ERROR.
    """
    if not isinstance(raw, dict) or set(raw) != {"timezone", "entries"}:
        raise ScheduleValidationError("store must contain exactly timezone and entries")
    timezone = raw["timezone"]
    if not isinstance(timezone, str) or not is_valid_timezone(timezone):
        raise ScheduleValidationError(f"invalid timezone {timezone!r}")
    raw_entries = raw["entries"]
    if not isinstance(raw_entries, list):
        raise ScheduleValidationError("entries must be a list")
    if len(raw_entries) > MAX_ENTRIES:
        raise ScheduleValidationError(f"entry cap is {MAX_ENTRIES}")
    entries = tuple(_parse_entry(entry, schedule_path.parent) for entry in raw_entries)
    ids = [entry.id for entry in entries]
    if len(set(ids)) != len(ids):
        raise ScheduleValidationError("entry ids must be unique")
    daily = sum(
        _cron_firings_per_day(entry.cron) for entry in entries if entry.cron is not None
    )
    if daily > MAX_DAILY_FIRINGS:
        raise ScheduleValidationError(
            f"store fires up to {daily} times a day; the cap is {MAX_DAILY_FIRINGS}"
        )
    # Exercise the zone conversion HERE, because it is the last thing that
    # can reject an entry: a day-of-month or month restriction that crosses
    # midnight in system-local time has no five-field spelling. Raised from
    # render() instead, it would be invisible -- the store would validate,
    # the model would see a clean file, and EVERY later render would fail,
    # silently freezing the crontab for all entries rather than just this
    # one (/code-review medium, 2026-08-31). Doing it here keeps a bad entry
    # on the same loud, all-or-nothing path as every other rejection.
    #
    # It does mean a store can stop being accepted without being edited: the
    # conversion depends on today's offset, so a DST change could newly push
    # a near-midnight monthly entry across a day boundary. Loud beats
    # silent, and the ERROR names the expression.
    store_zone = ZoneInfo(timezone)
    for entry in entries:
        if entry.cron is not None:
            _recurring_lines(entry.cron, store_zone)
    return Schedules(timezone=timezone, entries=entries)


def rejection_reason(path: str | os.PathLike[str]) -> str | None:
    """Why `load` would reject this store, or None if it is fine (or absent).

    Exists so the rejection can reach the MODEL and not just the journal.
    `sync_after_turn` treats a `None` from `load` as "nothing to install" and
    returns, which is right for a box with no schedules and quietly wrong for a
    box whose store is broken: on 2026-09-06 Marlow wrote an `at`+`job` entry,
    the store was refused on every turn for half an hour, the crontab stayed
    frozen at its last good version, and the only trace was an ERROR nobody was
    reading. Marlow had written the file successfully and had every reason to
    believe the job was live.

    Runs the real validator rather than re-deriving the rules, but does NOT log
    -- `load` already logged this once on the turn that installed nothing, and a
    second line per turn saying the same thing is what makes an ERROR ignorable.
    """
    schedule_path = Path(path)
    if not schedule_path.exists():
        return None
    try:
        _parse_store(json.loads(schedule_path.read_text()), schedule_path)
    except Exception as exc:
        return str(exc)
    return None


def _local_fields(instant: datetime) -> time.struct_time:
    """An instant in SYSTEM-LOCAL time, which is the only time cron understands.

    Deliberately libc's `localtime` rather than a tzinfo captured from
    `datetime.now().astimezone()`: the latter is a FIXED offset taken now, so a
    reminder set for six months out would render an hour wrong across the local
    zone's own DST change. `localtime` applies the rules for the instant given.
    """
    return time.localtime(instant.timestamp())


def _shift_weekdays(field: str, days: int) -> str:
    """Rotate a cron weekday field, normalising cron's two spellings of Sunday.

    Only reached when converting to system-local time crosses midnight, e.g. a
    Monday 07:33 in Europe is a Sunday evening in Alberta.
    """
    shifted = sorted({(value % 7 + days) % 7 for value in _cron_values(field, 0, 7)})
    return ",".join(str(value) for value in shifted)


def _recurring_lines(expression: str, store_zone: ZoneInfo) -> list[str]:
    """One cron expression in the store's zone -> its system-local equivalents.

    Debian's cron does not implement `CRON_TZ` (defect 1, 2026-08-30 -- it is
    not in `man 5 crontab` and the only timezone string in /usr/sbin/cron is
    `localtime`), so a zone-following schedule has to be converted HERE. The
    line cron gets is absolute and carries no zone at all.

    Fractional offsets are why this cannot be an hour shift: Newfoundland is
    -03:30 from Alberta, so the minute field moves too and a single expression
    can land on several local hours. Times are therefore expanded, converted
    individually, and regrouped one line per local hour -- fewer clever, more
    obviously right.
    """
    minute_field, hour_field, day_field, month_field, weekday_field = expression.split()
    # Today in the store's zone. The offset between two zones is a property of
    # the instant, so a reference date is unavoidable; this one is re-rendered
    # every turn, which is what makes a DST transition self-correct (see the
    # caveat in docs/tasks/schedules-and-reminders.md).
    reference = datetime.now(store_zone).date()

    by_day_then_hour: dict[int, dict[int, set[int]]] = {}
    for hour in sorted(_cron_values(hour_field, 0, 23)):
        for minute in sorted(_cron_values(minute_field, 0, 59)):
            local = _local_fields(
                datetime(reference.year, reference.month, reference.day, hour, minute, tzinfo=store_zone)
            )
            day_delta = (date(local.tm_year, local.tm_mon, local.tm_mday) - reference).days
            by_day_then_hour.setdefault(day_delta, {}).setdefault(local.tm_hour, set()).add(local.tm_min)

    lines: list[str] = []
    for day_delta, hours in sorted(by_day_then_hour.items()):
        day, month, weekday = day_field, month_field, weekday_field
        if day_delta:
            if day_field != "*" or month_field != "*":
                # A day-of-month or month restriction cannot be rotated: "the
                # 1st, 20:00 in Sydney" is the previous month's last day in
                # Alberta, which no five-field expression says. Refuse rather
                # than silently fire on the wrong date -- one bad entry aborts
                # the whole render, per this module's contract.
                raise ScheduleValidationError(
                    f"cron {expression!r} crosses a day boundary in system-local time, "
                    "which a day-of-month or month restriction cannot express"
                )
            if weekday_field != "*":
                weekday = _shift_weekdays(weekday_field, day_delta)
        for hour, minutes in sorted(hours.items()):
            minute_list = ",".join(str(value) for value in sorted(minutes))
            lines.append(f"{minute_list} {hour} {day} {month} {weekday}")
    return lines


def plumbing_crons(day_end: str) -> list[tuple[str, str]]:
    """(job, daily cron in the store's zone) for each harness job, from the
    end of "Your day". `day_end` is already validated (preferences.day())."""
    end = minutes(day_end)
    crons = []
    for job, offset in PLUMBING_OFFSETS:
        at = (end + offset) % (24 * 60)
        crons.append((job, f"{at % 60} {at // 60} * * *"))
    return crons


def checkin_crons(day_start: str, day_end: str, every_hours: int) -> list[tuple[str, str]]:
    """The check-in's daily cron, or nothing when it's off (0).

    At start + N, start + 2N, ... while there are at least
    CHECKIN_MIN_BEFORE_END_MINUTES left in the day. With 08:00-23:00 and N=3
    that is 11, 14, 17, 20: the schedule every box ran before. Measured from
    the start, so a day that ends after midnight works the same way. One line,
    because every check-in shares the start's minute.
    """
    if every_hours <= 0:
        return []
    start = minutes(day_start)
    length = (minutes(day_end) - start) % (24 * 60)
    hours = []
    offset = every_hours * 60
    while offset <= length - CHECKIN_MIN_BEFORE_END_MINUTES:
        hours.append(((start + offset) // 60) % 24)
        offset += every_hours * 60
    if not hours:
        return []
    return [(CHECKIN_JOB, f"{start % 60} {','.join(str(h) for h in sorted(hours))} * * *")]


def harness_crons(store: PreferencesStore) -> list[tuple[str, str]]:
    """Every line the renderer adds itself, from the preferences. Never
    raises: `day()` and `checkin_every_hours()` fall back to the defaults."""
    day_start, day_end = store.day()
    return plumbing_crons(day_end) + checkin_crons(day_start, day_end, store.checkin_every_hours())


def render(schedules: Schedules | None, harness: list[tuple[str, str]] | tuple = ()) -> str:
    """Render the complete crontab, in system-local time, from validated data.

    No `CRON_TZ` line: this cron ignores it, and emitting one would only be a
    second place for the zone to be wrong.

    Every conversion here was already exercised by load(), so an entry that
    would be inexpressible in five fields never reaches this function.

    `harness` is the lines the harness adds itself (`harness_crons`), in the
    store's zone like every entry. With no store at all they render in
    system-local time: there is no zone to follow, and a box with no schedules
    must still tidy up overnight. An every-day line never crosses a day
    boundary that five fields can't express, so these can't fail the render.
    """
    store_zone = ZoneInfo(schedules.timezone) if schedules is not None else None
    lines = [*_crontab_preamble(), ""]
    for job, expression in harness:
        local = _recurring_lines(expression, store_zone) if store_zone is not None else [expression]
        for line in local:
            lines.append(f"{line}  {_command()} {job}")
    for entry in (schedules.entries if schedules is not None else ()):
        # A recurring job keeps the direct `<job-name>` form; EVERYTHING
        # one-off goes through `--entry`, job or not. That is not cosmetic:
        # pruning lives on the --entry path, and a cron line carries no year,
        # so a one-off rendered in the direct form fires again on the same date
        # next year with nothing to remove it. `at`+`job` was refused outright
        # until 2026-09-06 for exactly this reason -- the fix is to render it
        # correctly rather than to forbid the shape Marlow kept reaching for.
        one_off = entry.at is not None
        argument = f"--entry {entry.id}" if one_off or entry.job is None else entry.job
        if entry.cron is not None:
            expressions = _recurring_lines(entry.cron, store_zone)
        else:
            local = _local_fields(at_instant(entry.at or "", schedules.timezone))
            expressions = [f"{local.tm_min} {local.tm_hour} {local.tm_mday} {local.tm_mon} *"]
        for expression in expressions:
            lines.append(f"{expression}  {_command()} {argument}")
    return "\n".join(lines) + "\n"


_ENV_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def foreign_command_lines(crontab_text: str) -> list[str]:
    """Installed lines this renderer could never have emitted.

    The renderer installs the COMPLETE crontab, so anything already there that
    is not one of its two line forms is destroyed on the first install. On the
    real box that was five `entrypoint.py <job>` lines plus a promoted
    `trello-sync.sh` and the identity pusher (review 2026-08-30) -- silently
    dropping the morning scan is the exact v1 failure this whole feature
    exists to prevent.

    So ownership is conditional: a crontab containing a line the renderer
    cannot express is not ours to replace, and install() refuses rather than
    clobbering it. Deliberately NOT a diff of job names -- Marlow removing an
    entry it owns is a legitimate edit (see "Risks accepted" in the task doc),
    while a line in a shape we cannot round-trip means the migration in
    deploy/README.md 6a has not been done.
    """
    foreign: list[str] = []
    for raw_line in crontab_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or _ENV_ASSIGNMENT_RE.match(line):
            continue
        fields = line.split(maxsplit=5)
        if len(fields) < 6 or not fields[5].startswith(f"{_command()} "):
            foreign.append(line)
    return foreign


def _run_crontab(argv: list[str], stdin_text: str | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    return subprocess.run(
        argv,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=_INSTALL_TIMEOUT_SECONDS,
        env=env,
        check=False,
    )


def install(text: str) -> bool:
    """Replace the current user's crontab atomically. Never raises."""
    try:
        current_result = _run_crontab(["crontab", "-l"])
        if current_result.returncode == 0:
            current = current_result.stdout
        elif current_result.returncode == 1 and "no crontab for" in current_result.stderr.lower():
            current = ""
        else:
            logger.error(
                "could not read installed crontab (exit=%s): %s",
                current_result.returncode,
                current_result.stderr.strip(),
            )
            return False
        # Byte-for-byte, which is what keeps the renderer idempotent: without
        # this the crontab is rewritten on EVERY turn. It assumes `crontab -l`
        # hands back exactly what was installed -- not universal (Vixie cron
        # prepends a DO NOT EDIT header), so it was probed on the box
        # 2026-08-30 via deploy/README.md 6c step 1: ROUNDTRIP-OK. Re-run that
        # one-liner before trusting this on any other host.
        if current == text:
            return True

        foreign = foreign_command_lines(current)
        if foreign:
            logger.error(
                "refusing to install the rendered crontab: %d installed line(s) are not "
                "renderer-owned and would be destroyed. Migrate them per deploy/README.md "
                "6a (a job into cron/schedules.json, a promoted script into its own systemd "
                "timer) and retry. Offending line(s): %s",
                len(foreign),
                "; ".join(foreign),
            )
            return False

        # `crontab -` reads the table from STDIN. Deliberately not a temp file
        # handed over by path: /security-review 2026-08-30 found that the
        # previous NamedTemporaryFile landed in the unit's /tmp, which the
        # sandbox shares read-write (bwrap is `--dev-bind / /` with no
        # `--tmpfs /tmp`) under the SAME uid -- so any `marlow` process could
        # rewrite the file in the window between the write and the exec, and
        # the harness would install a table it never rendered. Every guarantee
        # in this module (allowlisted ids, no command field, foreign line
        # refusal) is downstream of that file. A pipe has no name to race for.
        result = _run_crontab(["crontab", "-"], stdin_text=text)
        if result.returncode != 0:
            logger.error(
                "could not install rendered crontab (exit=%s): %s",
                result.returncode,
                result.stderr.strip(),
            )
            return False

        diff = "".join(
            difflib.unified_diff(
                current.splitlines(keepends=True),
                text.splitlines(keepends=True),
                fromfile="installed-crontab",
                tofile="rendered-crontab",
            )
        )
        logger.info("installed schedule crontab diff:\n%s", diff.rstrip())
        return True
    except Exception:
        logger.exception("schedule crontab install failed; leaving the previous crontab in place")
        return False


def prune_fired(path: str | os.PathLike[str], entry_id: str) -> bool:
    """Remove exactly one entry from the store, preserving survivor order."""
    schedule_path = Path(path)
    try:
        if load(schedule_path) is None:
            return False
        raw = json.loads(schedule_path.read_text())
        remaining = [entry for entry in raw["entries"] if entry.get("id") != entry_id]
        if len(remaining) == len(raw["entries"]):
            return False
        raw["entries"] = remaining
        schedule_path.write_text(json.dumps(raw, indent=2) + "\n")
        logger.info("pruned fired schedule entry %s", entry_id)
        return True
    except Exception:
        logger.exception("could not prune fired schedule entry %s", entry_id)
        return False


def _is_floating(at_value: str) -> bool:
    try:
        datetime.strptime(at_value, _AT_PINNED_FORMAT)
    except ValueError:
        return True
    return False


def pin_one_offs(path: str | os.PathLike[str], schedules: Schedules) -> Schedules:
    """Resolve floating one-offs to instants, in place, and re-read the store.

    The model writes "remind me at 22:14" as a wall-clock string, which is the
    right thing for it to write and the wrong thing to KEEP: it is re-resolved
    against the store's `timezone` at fire time, so editing that field retimes
    every pending reminder. On 2026-08-30 a 22:14 Newfoundland reminder became
    22:14 Alberta and woke Mike at 1:44am.

    Recurring entries are deliberately NOT pinned. "07:45 wherever I am" is
    what a repeating schedule means, and following the zone is the feature.
    """
    floating = {
        entry.id: entry.at
        for entry in schedules.entries
        if entry.at is not None and _is_floating(entry.at)
    }
    if not floating:
        return schedules

    schedule_path = Path(path)
    try:
        raw = json.loads(schedule_path.read_text())
        for raw_entry in raw["entries"]:
            value = floating.get(raw_entry.get("id"))
            if value is not None:
                raw_entry["at"] = at_instant(value, schedules.timezone).strftime(_AT_PINNED_FORMAT)
        schedule_path.write_text(json.dumps(raw, indent=2) + "\n")
        logger.info("pinned %d one-off schedule entry/entries to an instant", len(floating))
    except Exception:
        logger.exception("could not pin one-off schedule entries; leaving them floating")
        return schedules

    # Re-read rather than patching the in-memory copy: the store is the
    # security boundary, and what gets rendered should be what survived the
    # validator on the bytes actually on disk.
    return load(schedule_path) or schedules


def sync_after_turn(path: str | os.PathLike[str] | None = None) -> None:
    """Validate, pin, render, and install the store without ever failing a turn.

    ``path`` exists for the cron entrypoint, which knows the store it just
    pruned; the server always means SCHEDULES_PATH.
    """
    schedules_path = Path(path) if path is not None else SCHEDULES_PATH
    try:
        # Never raises: an unreadable preferences file is the default day.
        harness = harness_crons(preferences_store)
        schedules = load(schedules_path)
        if schedules is None:
            if schedules_path.exists():
                # Rejected: leave the last good crontab, harness lines and
                # all, rather than drop every reminder over one bad edit. The
                # model is told why on its next turn (`rejection_reason`). A
                # "Your day" change made meanwhile waits for the fix.
                return
            install(render(None, harness))
            return
        install(render(pin_one_offs(schedules_path, schedules), harness))
    except Exception:
        logger.exception("schedule sync failed; leaving the previous crontab in place")
