"""Where the user's device last said they were (docs/tasks/message-location.md).

Precise in memory, ROUNDED on disk. Until 2026-10-13 it was memory only, so a
restart left the morning brief with no position at all and the weather tool
(docs/tasks/weather-mcp.md) nothing to forecast. Mike's call that day: keep a
copy that survives a restart, coarse enough that it is never a home address.
One decimal place is ~11 km of latitude -- plenty for a forecast, nowhere near
a house -- and the place string is not written at all, since at Precise it can
name a neighbourhood. The file is in data_dir, which bwrap hides from the
agent, and devices are keyed by a hash of their token, not the token.

A message without a location clears its device's entry. That one rule covers
the setting being Off, a denied permission and no fresh fix, with no separate
"clear" call to get wrong.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from server.config import settings
from server.log import get_logger

logger = get_logger(__name__)

# Past this, a last-known position says more about where they were than where
# they are. 36 h, not the original 12 (Mike, 2026-10-13): the 7am brief after a
# quiet evening fell outside 12 h and lost the forecast's coordinates. The
# context line states the age, so the model can still judge an old one.
MAX_AGE_SECONDS = 36 * 60 * 60

# What survives a restart: see the module docstring.
DISK_DECIMALS = 1
DISK_ACCURACY_M = 10_000.0

# Free text that reaches the prompt. Long enough for "Neighbourhood, City,
# Province, Country" in any language Apple's geocoder returns.
MAX_PLACE_CHARS = 120

# `[` and `]` are the context block's own delimiters; control characters
# include the newline that would let a place name start a line of its own.
_UNSAFE_PLACE = re.compile(r"[\x00-\x1f\x7f-\x9f\[\]]")


@dataclass(frozen=True)
class Location:
    latitude: float
    longitude: float
    accuracy_m: float | None = None
    place: str | None = None


def clean_place(raw: str | None) -> str | None:
    """`raw` made safe to print inside the context block, or None if nothing
    is left."""
    if raw is None:
        return None
    text = " ".join(_UNSAFE_PLACE.sub(" ", raw).split())
    if len(text) > MAX_PLACE_CHARS:
        text = text[: MAX_PLACE_CHARS - 1].rstrip() + "…"
    return text or None


def validate(
    latitude: float, longitude: float, accuracy_m: float | None, place: str | None
) -> Location | None:
    """A Location, or None if the coordinates are not a place on Earth."""
    if not (math.isfinite(latitude) and math.isfinite(longitude)):
        return None
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return None
    if accuracy_m is not None and not (math.isfinite(accuracy_m) and accuracy_m > 0):
        # The accuracy is a nicety; a bad one costs the radius, not the fix.
        accuracy_m = None
    return Location(latitude, longitude, accuracy_m, clean_place(place))


def _device_key(device_token: str) -> str:
    return hashlib.sha256(device_token.encode()).hexdigest()[:16]


class LocationStore:
    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._by_device: dict[str, tuple[Location, float]] = {}
        self._path = path
        self._loaded = False

    def _file(self) -> Path:
        return self._path if self._path is not None else settings.last_location_path

    def _load(self) -> None:
        """Restore the rounded entries once, on first use. Caller holds the lock."""
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = json.loads(self._file().read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            logger.warning("ignoring unreadable last-location file: %s", e)
            return
        cutoff = time.time() - MAX_AGE_SECONDS
        for key, entry in (raw.items() if isinstance(raw, dict) else ()):
            try:
                at = float(entry["at"])
                where = validate(float(entry["latitude"]), float(entry["longitude"]), DISK_ACCURACY_M, None)
            except (KeyError, TypeError, ValueError):
                continue
            if where is not None and at >= cutoff:
                self._by_device.setdefault(key, (where, at))

    def _save(self) -> None:
        """Write the rounded copy. Caller holds the lock. A failure costs the
        restart case, never the message, so it logs rather than raises."""
        payload = {
            key: {
                "latitude": round(where.latitude, DISK_DECIMALS),
                "longitude": round(where.longitude, DISK_DECIMALS),
                "at": round(at),
            }
            for key, (where, at) in self._by_device.items()
        }
        path = self._file()
        try:
            if not payload:
                path.unlink(missing_ok=True)
                return
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload))
            tmp.chmod(0o600)
            os.replace(tmp, path)
        except OSError as e:
            logger.error("could not persist last location to %s: %s", path, e)

    def report(self, device_token: str, location: Location | None, *, now: float | None = None) -> None:
        key = _device_key(device_token)
        with self._lock:
            self._load()
            if location is None:
                if self._by_device.pop(key, None) is None:
                    return  # nothing held, nothing to rewrite
            else:
                self._by_device[key] = (location, time.time() if now is None else now)
            self._save()

    def latest(self) -> tuple[Location, float] | None:
        """The newest report across devices and when it arrived (epoch
        seconds). Cron turns have no device of their own, so "most recent
        wins", as for the timezone (`pairing_store.latest_timezone`)."""
        with self._lock:
            self._load()
            if not self._by_device:
                return None
            return max(self._by_device.values(), key=lambda entry: entry[1])

    def _reset_for_tests(self) -> None:
        with self._lock:
            self._by_device.clear()
            self._loaded = True
            try:
                self._file().unlink(missing_ok=True)
            except OSError:
                pass


location_store = LocationStore()
