"""Choices about Marlow that every client shares (/v1/preferences).

The face and the Thinking level. Until 2026-10-16 both were per device, so web
and iOS could show different Marlows that thought differently; there is one
assistant, so the box holds one of each (Mike). Clients still keep a local
copy and send the effort with every message (docs/tasks/model-and-effort-
config.md): this store is what keeps those copies in step, not a new source
the turn reads.

The face is an id and nothing else: which ids exist, what each looks like and
how it is cropped are the clients' (lib/faces.ts, MarlowFace.swift), so a new
face never needs a box deploy, and an id a client doesn't know falls back on
that client. A loose bound keeps a junk value out of the file.

"Your day" (docs/tasks/default-infrastructure.md) lives here too, for a
stronger reason than the other two: it times the harness's own overnight jobs,
so it must have exactly one writer, the user through Settings. Were it in an
agent-writable file, "stop the 1am message" could move the session rollover.

In data_dir with the device tokens: server-private, hard-denied for Read and
tmpfs'd out of the bwrap namespace. Nothing secret, but the agent has no use
for it either.
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from server.config import settings
from server.log import get_logger

logger = get_logger(__name__)

MAX_FACE_ID = 999
# The clients' three levels (ThinkingEffort.swift, chatStore's Effort).
EFFORTS = ("low", "medium", "high")

# "Your day", local wall-clock "HH:MM" in the schedule store's timezone. The
# defaults reproduce the times the harness used before the setting existed
# (consolidate at 01:00 = 23:00 + 2h), so a box that never opens Settings
# behaves as it always did.
DEFAULT_DAY_START = "08:00"
DEFAULT_DAY_END = "23:00"

# The check-in (docs/tasks/default-infrastructure.md tier 2): every N hours
# inside "Your day", or 0 for off. 3 with the default day gives 11, 14, 17, 20,
# the times every box ran before the setting existed.
DEFAULT_CHECKIN_EVERY_HOURS = 3
MAX_CHECKIN_EVERY_HOURS = 6

_HHMM = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])\Z")
_DAY_MINUTES = 24 * 60
# The night must hold both tidy-up turns (end + 2h) with margin before the
# next day's first job.
MIN_NIGHT_MINUTES = 4 * 60


class DayError(ValueError):
    """A "Your day" that can't be accepted. `code` is for the clients, which
    word it; the server sends facts, not copy."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def minutes(hhmm: str) -> int:
    """"HH:MM" -> minutes after midnight. Raises DayError("bad_time")."""
    match = _HHMM.fullmatch(hhmm) if isinstance(hhmm, str) else None
    if match is None:
        raise DayError("bad_time")
    return int(match.group(1)) * 60 + int(match.group(2))


def validate_day(start: str, end: str) -> None:
    """Raises DayError unless start..end is a day the harness can time.

    Start between 04:00 and 12:00. End between 20:00 and 03:00, where an end
    before noon is the next morning: a night owl's "my day goes until 2am"
    (Mike, 2026-10-21). And the night, end to the next start, at least
    MIN_NIGHT_MINUTES.
    """
    s, e = minutes(start), minutes(end)
    if not 4 * 60 <= s <= 12 * 60:
        raise DayError("day_start_range")
    if not (e >= 20 * 60 or e <= 3 * 60):
        raise DayError("day_end_range")
    if (s - e) % _DAY_MINUTES < MIN_NIGHT_MINUTES:
        raise DayError("night_too_short")


class PreferencesStore:
    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._path = path

    def _file(self) -> Path:
        return self._path if self._path is not None else settings.preferences_path

    def _read(self) -> dict:
        """The stored preferences; an unreadable file reads as none, so a bad
        write costs the choice, never the request."""
        try:
            raw = json.loads(self._file().read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            logger.warning("ignoring unreadable preferences file: %s", e)
            return {}
        return raw if isinstance(raw, dict) else {}

    def face(self) -> int | None:
        with self._lock:
            value = self._read().get("face")
        # bool is an int to isinstance; true is not face 1.
        if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_FACE_ID:
            return value
        return None

    def effort(self) -> str | None:
        with self._lock:
            value = self._read().get("effort")
        return value if value in EFFORTS else None

    def day(self) -> tuple[str, str]:
        """(start, end) of "Your day", defaults filled in. Never raises: the
        overnight jobs render from this, and an unreadable or invalid file
        must still give them a time (the defaults), not skip them."""
        with self._lock:
            data = self._read()
        start = data.get("day_start", DEFAULT_DAY_START)
        end = data.get("day_end", DEFAULT_DAY_END)
        try:
            validate_day(start, end)
        except DayError:
            logger.warning("ignoring invalid stored day %r-%r; using the defaults", start, end)
            return DEFAULT_DAY_START, DEFAULT_DAY_END
        return start, end

    def checkin_every_hours(self) -> int:
        """The check-in interval, 0 for off. Never raises, like `day()`."""
        with self._lock:
            value = self._read().get("checkin_every_hours", DEFAULT_CHECKIN_EVERY_HOURS)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_CHECKIN_EVERY_HOURS:
            return value
        return DEFAULT_CHECKIN_EVERY_HOURS

    def checkin_is_set(self) -> bool:
        with self._lock:
            return "checkin_every_hours" in self._read()

    def day_is_set(self) -> bool:
        """Whether either end of the day was ever chosen (Settings, or the
        migration that seeded it from a box's old schedule)."""
        with self._lock:
            data = self._read()
        return "day_start" in data or "day_end" in data

    def update(
        self,
        *,
        face: int | None = None,
        effort: str | None = None,
        day_start: str | None = None,
        day_end: str | None = None,
        checkin_every_hours: int | None = None,
    ) -> None:
        """Sets what is given and keeps the rest. Raises OSError if it can't
        be written: the client keeps its choice and sends it again, so it must
        not think this one landed. Raises DayError, writing nothing, if the
        day it would leave is invalid -- judged on the pair, since a new end
        can only be checked against the start it will sit with."""
        with self._lock:
            data = self._read()
            if day_start is not None or day_end is not None:
                start = day_start if day_start is not None else data.get("day_start", DEFAULT_DAY_START)
                end = day_end if day_end is not None else data.get("day_end", DEFAULT_DAY_END)
                validate_day(start, end)
                # Only what was sent: an unset field keeps meaning "the
                # default", so a later default still reaches it.
                if day_start is not None:
                    data["day_start"] = day_start
                if day_end is not None:
                    data["day_end"] = day_end
            if checkin_every_hours is not None:
                # The endpoint bounds it; this is for the migration's sake.
                if not 0 <= checkin_every_hours <= MAX_CHECKIN_EVERY_HOURS:
                    raise ValueError(f"checkin_every_hours out of range: {checkin_every_hours}")
                data["checkin_every_hours"] = checkin_every_hours
            if face is not None:
                data["face"] = face
            if effort is not None:
                data["effort"] = effort
            path = self._file()
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data))
            tmp.chmod(0o600)
            os.replace(tmp, path)

    def _reset_for_tests(self) -> None:
        with self._lock:
            try:
                self._file().unlink(missing_ok=True)
            except OSError:
                pass


preferences_store = PreferencesStore()
