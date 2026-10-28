"""Weather for Marlow: a stdio MCP server over Open-Meteo (docs/tasks/weather-mcp.md).

WebSearch answered a week of Hwy 17 weather with a July Thunder Bay page and
26°C for the Soo in October (2026-10-11). Stale or mixed-date data is the bug,
so every line this server prints names its own date, in the location's local
time, and a date the forecast does not cover is refused rather than guessed.

STANDALONE ON PURPOSE: stdlib only, no `server.*` imports. The CLI spawns it
with cwd = Marlow's working dir, which the agent writes, so invoke.py runs it
as `python -I <this file>` -- isolated mode keeps that directory off sys.path,
and an import of our own package would need it back on.

The transport is MCP's stdio framing by hand: one JSON-RPC message per line.
Three methods matter (`initialize`, `tools/list`, `tools/call`); that is small
enough that the `mcp` SDK would be the larger dependency surface.
"""
from __future__ import annotations

import bisect
import gzip
import json
import logging
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
# A healthy answer takes ~0.4 s, even a 7-stop route (box, 2026-10-13).
TIMEOUT_SECONDS = 5
RETRY_DELAY_SECONDS = 1
RETRY_AFTER_MAX_SECONDS = 3
ALERT_TIMEOUT_SECONDS = 4
ALERT_WORKERS = 6
ALERT_BUDGET_SECONDS = 20
FALLBACK_BUDGET_SECONDS = 45
MAX_DAYS = 16
MAX_STOPS = 12
GEOCODE_RESULTS = 5

# Coordinates are rounded before they leave the box (Mike, 2026-10-13): a
# Precise fix is ±2 m, i.e. a house, and Open-Meteo has no need of one. Two
# decimals is ~1 km -- finer than any model grid it serves (HRDPS is 2.5 km),
# so the forecast is unchanged. Coarser was considered and refused: on a lake
# shore 10 km is the difference between the town and open water.
COORD_DECIMALS = 2

# GEM first, Open-Meteo's own pick after it runs out. `best_match` over
# Ontario is NOAA's GFS, not Environment Canada (measured 2026-10-13: its
# numbers were identical to gfs_seamless, and different from gem_seamless),
# while GEM was the reason Open-Meteo was chosen. GEM stops at ~9 days, so
# each ROW takes every value from one model -- GEM where it has the row,
# best_match where it does not -- and says which. Never mixed within a row.
MODELS = ("gem_seamless", "best_match")
MODEL_LABELS = {"gem_seamless": "GEM", "best_match": "best match"}

DAILY_VARS = (
    "weather_code", "temperature_2m_max", "temperature_2m_min",
    "precipitation_sum", "precipitation_probability_max", "snowfall_sum",
    "wind_speed_10m_max", "wind_gusts_10m_max", "wind_direction_10m_dominant",
    "sunrise", "sunset",
)
HOURLY_VARS = (
    "temperature_2m", "weather_code", "precipitation", "precipitation_probability", "snowfall",
    "wind_speed_10m", "wind_gusts_10m", "wind_direction_10m",
)

# The shape of an answer (Mike, 2026-10-13, after the camping trip's feedback):
# day and night PERIODS for the first week, as Environment Canada and the NWS
# publish them, then one line per day. A night runs to 06:00 the next morning,
# so "tonight" carries its real low -- the overnight-at-camp question answered
# by the default shape, with no camping parameter in the schema. Hour by hour
# only on request and only for today and tomorrow: ~48 h is how far the
# high-resolution model runs, and past it an hourly figure is false precision
# that the model would repeat with confidence. A route stop's arrival hour
# is one line, not a day of them, so it reaches 72 h from now, inside the
# regional GEM's ~84: by calendar day, a Tuesday-evening plan lost Thursday
# morning's arrival, 41 h away (Mike, 2026-10-13).
PERIOD_DAYS = 7
ARRIVAL_HOUR_MAX_HOURS = 72
DAY_START, NIGHT_START = 6, 18
HOURS_CHOICES = ("today", "tomorrow")

# Past 16 days there is no forecast, but there is still an honest answer.
BEYOND = "for dates that far out, typical conditions for the month are the best guide"

# WMO weather interpretation codes, as Open-Meteo documents them.
WMO = {
    0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "light freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "light freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    85: "light snow showers", 86: "snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}
COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
DASH = "–"  # a missing value is shown as missing, never as 0

_UNSAFE_TEXT = re.compile(r"[\x00-\x1f\x7f-\x9f]")

SERVER_INFO = {"name": "weather", "version": "1"}
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


class ToolError(Exception):
    """A problem worth one line back to the model, never a traceback."""


# --- the problem log -------------------------------------------------------------
#
# Every source here is someone else's service with no promise to us (Mike,
# 2026-10-13): when Environment Canada moves a URL or renames a field, this file
# is where it shows. So: problems only -- a failed fetch, a fallback used, a
# response of the wrong shape, and the quieter kind, a response that parsed but
# yielded nothing usable. That last kind is the dangerous one: a renamed
# `alert_name_en` would otherwise skip every warning and print "none in effect".
#
# Its own file, plain appends, not rotating, for the reasons in server/log.py's
# get_deny_logger: one short-lived process per `claude -p` call, so concurrent
# writers are possible and rotation is what makes them unsafe; quiet operation
# writes nothing, so size is the alarm. Coordinates are logged to 0.1° (~10 km),
# the precision the disk is allowed to hold (server/location.py).

log = logging.getLogger("marlow.weather")


def _where(lat: float, lon: float) -> str:
    return f"{lat:.1f},{lon:.1f}"


_PATH_COORD = re.compile(r"(-?[0-9]+[.][0-9])[0-9]+")


def _quiet_url(url: str) -> str:
    """Host and path, no query; coordinates in the path cut to 0.1°."""
    parts = urllib.parse.urlsplit(url)
    return (parts.hostname or "") + _PATH_COORD.sub(r"\g<1>", parts.path)


def _drift(source: str, what: str) -> None:
    """A response that parsed but did not look the way this code expects."""
    log.warning("DRIFT %s: %s", source, what)


# --- seams the tests replace ----------------------------------------------

USER_AGENT = "marlow-weather/1 (+https://github.com/mikemason/marlow-server)"


class _HTTPStatus(ToolError):
    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


class _Transient(_HTTPStatus):
    """Overload, rate limit, network: worth one more try, unless the service
    asked for a longer wait than a turn can spend."""

    def __init__(self, message: str, code: int, retry_after: float | None = None) -> None:
        super().__init__(message, code)
        self.retry_after = retry_after


class _Timeout(_Transient):
    """No answer in time. Not retried: a second wait comes out of the turn,
    and on 2026-10-13 neither afternoon outage answered a second attempt."""


def _retry_after(value: str | None) -> float | None:
    try:
        return max(float(value), 0.0) if value is not None else None
    except ValueError:
        return None  # an HTTP date: further off than a turn can wait anyway


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).hostname or "weather service"


def _fetch_once(url: str, timeout: float = TIMEOUT_SECONDS) -> Any:
    # NWS and MET Norway both require a User-Agent that identifies the caller.
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, application/geo+json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as e:
        # Open-Meteo explains a 400 in the body: {"error": true, "reason": ...}
        try:
            body = json.loads(e.read())
            # OSRM says why in "message" ("Impossible route between points").
            reason = _text(body.get("reason") or body.get("message"), 200)
        except Exception:
            reason = None
        message = f"{_host(url)} returned HTTP {e.code}" + (f": {reason}" if reason else "")
        if e.code == 429 or e.code >= 500:
            raise _Transient(message, e.code, _retry_after(e.headers.get("Retry-After") if e.headers else None)) from None
        raise _HTTPStatus(message, e.code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        # A connect timeout arrives wrapped in a URLError, a read timeout bare.
        if isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError):
            raise _Timeout(f"{_host(url)} unreachable (TimeoutError)", 0) from None
        raise _Transient(f"{_host(url)} unreachable ({type(e).__name__})", 0) from None
    except ValueError:
        raise ToolError(f"{_host(url)} returned something that is not JSON") from None


def _fetch_json(url: str, *, quick: bool = False) -> Any:
    # One retry, because it was needed on day one: 2026-10-13 ~08:05 EDT the
    # forecast host answered 503 "The service is overloaded" and 10 s timeouts
    # for 10-15 minutes while geocoding worked. Not more than one -- past that
    # the national fallbacks below are the better use of the wait. And only
    # for a quick failure (a 503 is back in a fraction of a second): a timeout
    # goes straight to the fallback, as does a Retry-After longer than a turn
    # should spend (Mike, 2026-10-13). A retry that works is logged, so the
    # log says whether the retry earns its place.
    # `quick` is for the alerts line, an add-on to an answer that is already
    # in hand: one short attempt, so an alert source's outage costs seconds.
    attempts = 1
    try:
        if quick:
            return _fetch_once(url, timeout=ALERT_TIMEOUT_SECONDS)
        try:
            return _fetch_once(url)
        except _Timeout:
            raise
        except _Transient as e:
            wait = RETRY_DELAY_SECONDS if e.retry_after is None else e.retry_after
            if wait > RETRY_AFTER_MAX_SECONDS:
                raise
            _sleep(wait)
            attempts = 2
            result = _fetch_once(url)
            log.warning("RETRY ok %s after: %s", _quiet_url(url), e)
            return result
    except ToolError as e:
        # NWS answers 404 for every point outside the US; that is an answer.
        if not (isinstance(e, _HTTPStatus) and e.code == 404 and url.startswith(NWS_URL)):
            tried = "1 quick attempt" if quick else f"{attempts} attempt{'s' if attempts > 1 else ''}"
            log.warning("FETCH %s failed (%s): %s", _quiet_url(url), tried, e)
        raise


_sleep = time.sleep
_clock = time.monotonic


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- input checks ------------------------------------------------------------

def _number(args: dict, key: str, low: float, high: float) -> float:
    value = args.get(key)
    # bool is an int subclass; `true` is not a latitude.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError(f"{key} must be a number")
    if not math.isfinite(value) or not low <= value <= high:
        raise ToolError(f"{key} must be between {low:g} and {high:g}")
    return float(value)


def _coords(args: dict) -> tuple[float, float]:
    return (
        round(_number(args, "latitude", -90, 90), COORD_DECIMALS),
        round(_number(args, "longitude", -180, 180), COORD_DECIMALS),
    )


def _int(args: dict, key: str, low: int, high: int, default: int | None = None) -> int | None:
    value = args.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ToolError(f"{key} must be a whole number from {low} to {high}")
    return value


def _date(args: dict, key: str, required: bool) -> date | None:
    value = args.get(key)
    if value is None and not required:
        return None
    try:
        if isinstance(value, str):
            return date.fromisoformat(value)
    except ValueError:
        pass
    raise ToolError(f"{key} must be a date like 2026-10-14")


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(_UNSAFE_TEXT.sub(" ", value).split())[:limit]
    return text or None


# --- formatting ----------------------------------------------------------------

def _fmt(value: Any, unit: str = "", digits: int = 0) -> str:
    if value is None:
        return DASH
    text = f"{value:.{digits}f}"
    # -0.4°C rounds to "-0"; a low of "-0–9°C" reads as a typo (seen live).
    if text.lstrip("-").strip("0.") == "":
        text = text.lstrip("-")
    return f"{text}{unit}"


def _compass(degrees: Any) -> str:
    if degrees is None:
        return ""
    return COMPASS[int((degrees % 360) / 45 + 0.5) % 8] + " "


def _sky(code: Any) -> str:
    if code is None:
        return DASH
    return WMO.get(int(code), f"WMO code {int(code)}")


def _zone(point: dict) -> Any:
    """The location's tzinfo. Open-Meteo's own abbreviation is "GMT-4" where a
    person says "EDT", so zoneinfo names it when it can."""
    try:
        return ZoneInfo(point.get("timezone") or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _abbr(tz: Any, local_iso: str) -> str:
    """The abbreviation in force AT that local time, not now: an hourly row on
    the far side of a DST change must not borrow today's EDT."""
    return datetime.fromisoformat(local_iso).replace(tzinfo=tz).tzname() or "UTC"


def _weekday(iso: str) -> str:
    d = date.fromisoformat(iso[:10])
    return f"{d:%a} {d.isoformat()}"


def _pick(section: dict, i: int, probe: str) -> str:
    """Which model this row comes from: GEM if GEM has it."""
    for model in MODELS:
        values = section.get(f"{probe}_{model}")
        if values and i < len(values) and values[i] is not None:
            return model
    return MODELS[-1]


def _row(section: dict, i: int, model: str, var: str) -> Any:
    values = section.get(f"{var}_{model}") or []
    return values[i] if i < len(values) else None


def _snow(cm: Any) -> str:
    """Open-Meteo's snowfall is in cm. Said only when there is some: on the
    north shore in October, snow rather than rain is the line that matters."""
    return f", {cm:.1f} cm snow" if isinstance(cm, (int, float)) and cm > 0 else ""


# The amount and the chance come from different calculations, so 0.0 mm at a
# 95% chance is real model output, not a bug -- but unlabelled it read as one
# (Marlow's own feedback, 2026-10-13). Hence the words around both numbers.
def _wet(amount: Any, chance: Any, period: str) -> str:
    return f"{_fmt(amount, ' mm', 1)}{' ' + period if period else ''}, {_fmt(chance, '%')} chance"


def _daily_line(daily: dict, i: int) -> str:
    model = _pick(daily, i, "temperature_2m_max")
    v = lambda var: _row(daily, i, model, var)  # noqa: E731
    sunrise, sunset = v("sunrise"), v("sunset")
    sun = f", sun {sunrise[11:16]}–{sunset[11:16]}" if sunrise and sunset else ""
    return (
        f"{_weekday(daily['time'][i])}: "
        f"{_fmt(v('temperature_2m_min'))}–{_fmt(v('temperature_2m_max'))}°C, "
        # Open-Meteo's daily code is the day's MOST SEVERE condition, so
        # "heavy drizzle" sat beside an arrival hour of 0.4 mm and read as a
        # contradiction (Marlow, 2026-10-13). Saying "worst" removes it.
        f"worst: {_sky(v('weather_code'))}, "
        f"{_wet(v('precipitation_sum'), v('precipitation_probability_max'), 'total')}{_snow(v('snowfall_sum'))}, "
        f"wind {_compass(v('wind_direction_10m_dominant'))}{_fmt(v('wind_speed_10m_max'))} "
        f"gusting {_fmt(v('wind_gusts_10m_max'))} km/h{sun} [{MODEL_LABELS[model]}]"
    )


def _hourly_line(hourly: dict, i: int, tz: Any) -> str:
    """Temperature and wind at the hour; the condition, rain and its chance
    are Open-Meteo's for the hour ENDING then, and say so: "0.1 mm this hour"
    at 09:00 had Marlow placing 08-09's drizzle at 09-10 (2026-10-13)."""
    model = _pick(hourly, i, "temperature_2m")
    v = lambda var: _row(hourly, i, model, var)  # noqa: E731
    t = hourly["time"][i]
    return (
        f"{t[11:16]} {_abbr(tz, t)}: {_fmt(v('temperature_2m'))}°C, "
        f"wind {_compass(v('wind_direction_10m'))}{_fmt(v('wind_speed_10m'))} "
        f"gusting {_fmt(v('wind_gusts_10m'))} km/h; {_next_hour(t, -1)}–{t[11:16]}: "
        f"{_sky(v('weather_code'))}, "
        f"{_wet(v('precipitation'), v('precipitation_probability'), '')}{_snow(v('snowfall'))} [{MODEL_LABELS[model]}]"
    )


def _zone_span(tz: Any, first: str, last: str) -> str:
    """"EDT", or "EDT→EST" for a period across the change (review)."""
    a, b = _abbr(tz, first), _abbr(tz, last)
    return a if a == b else f"{a}→{b}"


def _hour_iso(day: date, hour: int) -> str:
    return f"{day.isoformat()}T{hour:02d}:00"


def _next_hour(t: str, step: int = 1) -> str:
    return (datetime.fromisoformat(t) + timedelta(hours=step)).isoformat()[11:16]


def _period_line(hourly: dict, tz: Any, day: date, night: bool, since: str, daily: dict | None = None) -> str | None:
    """One day (06-18) or night (18-06 next morning) as a summary of its hours
    from `since` on, or None when none of them are left. A period cut short by
    the clock or by the data says so: a night "low" from its first six hours
    is not a low."""
    start = _hour_iso(day, NIGHT_START if night else DAY_START)
    end = _hour_iso(day + timedelta(days=1), DAY_START) if night else _hour_iso(day, NIGHT_START)
    times = hourly.get("time") or []
    hours = [i for i, t in enumerate(times) if max(start, since) <= t < end]
    if not hours:
        return None
    # Open-Meteo's hourly precipitation at T is the hour ENDING at T, so a
    # period's rain is the values one step later than its temperatures.
    wet_hours = [i for i, t in enumerate(times) if max(start, since) < t <= end]
    first, last = times[hours[0]], times[hours[-1]]
    # The start shows when the clock cut it short ("day 14:00–18:00").
    span = f"{first[11:16]}–{end[11:16]}"
    last_wanted = (datetime.fromisoformat(end) - timedelta(hours=1)).isoformat()[:16]
    if last < last_wanted:
        span += f" (data ends {last[11:16]})"
    label = f"{_weekday(day.isoformat())} {'night' if night else 'day'}"

    model_of = {i: _pick(hourly, i, "temperature_2m") for i in hours}

    def values(var: str, among: list[int] = hours) -> list[tuple[int, Any]]:
        return [(i, x) for i in among
                if (x := _row(hourly, i, model_of.get(i) or _pick(hourly, i, "temperature_2m"), var)) is not None]

    temps = [x for _, x in values("temperature_2m")]
    gusts = [x for _, x in values("wind_gusts_10m")]
    chance = [x for _, x in values("precipitation_probability")]
    rain = [x for _, x in values("precipitation", wet_hours)]
    snow = [x for _, x in values("snowfall", wet_hours)]
    speeds = values("wind_speed_10m")
    windiest = max(speeds, key=lambda pair: pair[1]) if speeds else None
    bearing = _row(hourly, windiest[0], model_of[windiest[0]], "wind_direction_10m") if windiest else None
    # Up to three conditions in the order they come, but the three most
    # severe (WMO codes rise roughly with severity): a 12-hour day can hold
    # six, and the one cut must never be the thunderstorm. Each with when:
    # without it Marlow could not say when the rain comes, past the
    # hour-by-hour days. A code at T, like the rain, is the hour ending at T
    # (code 80 sits exactly on the hours with rain in it), so the same hours.
    coded = [(i, int(code)) for i, code in values("weather_code", wet_hours)]
    codes = list(dict.fromkeys(code for _, code in coded))
    worst = set(sorted(codes, reverse=True)[:3])

    def when(code: int) -> str:
        runs: list[list[str]] = []
        for at, (i, c) in enumerate(coded):
            if c == code:
                if at and coded[at - 1][1] == code:
                    runs[-1][1] = times[i]
                else:
                    runs.append([times[i], times[i]])
        if len(runs) > 2:
            return f"on and off {_next_hour(runs[0][0], -1)}–{runs[-1][1][11:16]}"
        return " and ".join(f"{_next_hour(a, -1)}–{b[11:16]}" for a, b in runs)

    skies = [f"{_sky(code)} {when(code)}" for code in codes if code in worst]
    # The hour a rain value ends, so its own hour starts one earlier.
    wettest = max(values("precipitation", wet_hours), key=lambda pair: pair[1], default=None)
    heaviest = ""
    if wettest and round(wettest[1], 1) > 0:
        ends = times[wettest[0]]
        heaviest = f" (heaviest {wettest[1]:.1f} mm {_next_hour(ends, -1)}–{ends[11:16]})"
    models = list(dict.fromkeys(MODEL_LABELS[model_of[i]] for i in hours))
    sun = ""
    if daily is not None and not night and day.isoformat() in (daily.get("time") or []):
        i = daily["time"].index(day.isoformat())
        model = _pick(daily, i, "sunrise")
        sunrise, sunset = _row(daily, i, model, "sunrise"), _row(daily, i, model, "sunset")
        sun = f", sun {sunrise[11:16]}–{sunset[11:16]}" if sunrise and sunset else ""
    return (
        f"{label} {span} {_zone_span(tz, first, last)}: "
        f"{_fmt(min(temps) if temps else None)}–{_fmt(max(temps) if temps else None)}°C, "
        f"{', '.join(skies) or DASH}, "
        f"{_fmt(sum(rain) if rain else None, ' mm', 1)} total{heaviest}, up to {_fmt(max(chance) if chance else None, '%')} "
        f"chance{_snow(sum(snow) if snow else None)}, "
        f"wind {_compass(bearing)}{_fmt(windiest[1] if windiest else None)} "
        f"gusting {_fmt(max(gusts) if gusts else None)} km/h{sun} [{' then '.join(models)}]"
    )


def _since(tz: Any) -> tuple[date, str]:
    """Today's local date, and the current hour as a time string: hours before
    it are history, not forecast."""
    local = _now().astimezone(tz)
    return local.date(), _hour_iso(local.date(), local.hour)


def _fetched(point: dict) -> str:
    local = _now().astimezone(_zone(point))
    return f"{local:%a %Y-%m-%d %H:%M} {local.tzname()}"


def _forecast_url(lats: list[float], lons: list[float], *, hourly: bool, days: int = MAX_DAYS) -> str:
    params = {
        "latitude": ",".join(f"{x:g}" for x in lats),
        "longitude": ",".join(f"{x:g}" for x in lons),
        "daily": ",".join(DAILY_VARS),
        "timezone": "auto",
        "forecast_days": str(max(1, min(days, MAX_DAYS))),
        "models": ",".join(MODELS),
    }
    if hourly:
        params["hourly"] = ",".join(HOURLY_VARS)
    return f"{FORECAST_URL}?{urllib.parse.urlencode(params, safe=',')}"


def _points(payload: Any, expected: int) -> list[dict]:
    """Open-Meteo answers one point with an object and several with a list."""
    points = payload if isinstance(payload, list) else [payload]
    if len(points) != expected or not all(isinstance(p, dict) and "daily" in p for p in points):
        _drift("open-meteo", f"expected {expected} point(s) with daily data, got "
                             f"{type(payload).__name__} of {len(points)}")
        raise ToolError("Open-Meteo returned an unexpected shape")
    return points


def _out_of_range(daily: dict, wanted: date) -> str | None:
    days = daily.get("time") or []
    if not days:
        return "no forecast days returned"
    if wanted.isoformat() < days[0]:
        return f"{wanted.isoformat()} is already past (forecast starts {days[0]})"
    if wanted.isoformat() > days[-1]:
        return f"{wanted.isoformat()} is beyond the forecast (it ends {days[-1]}; {BEYOND})"
    return None


# --- fallbacks: national services, inside the server ----------------------------
#
# Open-Meteo is free with no uptime promise, and on 2026-10-13 it was down for
# 10-15 minutes on the morning this server was written. Mike's call that day:
# the SERVER falls back, not the model -- an identity rule telling Marlow to go
# find a national weather page is one more thing for the LLM to coordinate, and
# a model left to improvise reaches for WebSearch, which is the original bug.
#
# Only on a transient Open-Meteo failure (5xx, 429, timeout, network), after
# its one retry; a 400 is our bug and falls back to nothing. Coverage is
# Canada, the UK and the US; elsewhere the answer is "unavailable". Fallbacks
# give less than Open-Meteo (day and night periods; ECCC ~6 days, NWS 7, MET
# Norway ~10) and every answer says which source it came from, so an outage is
# visible in the transcript rather than silent. ECCC's 24 hours of hourly
# forecast are used too, since Marlow read a morning block as "10am at Lake
# Louise" during an outage (2026-10-13); MET Norway's and the NWS's hourly
# data are still unwired.

ECCC_URL = "https://api.weather.gc.ca/collections/citypageweather-realtime/items"
# A city page is a regional forecast pinned to one town, and in the mountains
# the pins are far apart: ECCC pins its Valemount page on McBride, 80 km away,
# so the real Valemount and the Columbia Icefield (75 km from Jasper) got
# nothing at 40 km (2026-10-13). Within 40 km a page is "the nearest city
# page"; out to 100 km it is a regional guide and says so, and the NWS is
# asked first, so a US point near the border keeps its own forecast.
ECCC_RADIUS_KM = 40
ECCC_FAR_KM = 100
METNO_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
NWS_URL = "https://api.weather.gov/points"

# ECCC city pages carry UTC times and a province-prefixed id ("on-19"), not a
# zone. The province is enough for a forecast's dates and an issue time;
# Ontario west of 90°W (Kenora, not Thunder Bay) is the one split worth making.
PROVINCE_ZONES = {
    "ab": "America/Edmonton", "bc": "America/Vancouver", "mb": "America/Winnipeg",
    "nb": "America/Moncton", "nl": "America/St_Johns", "ns": "America/Halifax",
    "nt": "America/Yellowknife", "nu": "America/Iqaluit", "on": "America/Toronto",
    "pe": "America/Halifax", "qc": "America/Toronto", "sk": "America/Regina",
    "yt": "America/Whitehorse",
}
# Where a province's one zone is wrong often enough to matter: a longitude (and
# for BC, latitude) test, not a polygon. Atikokan and Creston (no DST) are left
# wrong -- an issue time an hour off in the summer, dates unaffected.
def _canadian_zone(province: str, lat: float, lon: float) -> str:
    if province == "on" and lon < -90:
        return "America/Winnipeg"            # Kenora, not Thunder Bay
    if province == "bc" and lat > 55.5 and lon > -122.5:
        return "America/Dawson_Creek"        # Peace region: MST all year
    if province == "qc" and lon > -63:
        return "America/Halifax"             # Magdalen Islands
    if province == "nu":
        if lon < -102:
            return "America/Cambridge_Bay"
        if lon < -85:
            return "America/Rankin_Inlet"
    return PROVINCE_ZONES.get(province, "America/Toronto")


# Mostly "on-19", but 32 Quebec pages are "qc-a0".."qc-d4" (live listing,
# 2026-10-13). Still nothing that could leave the path segment.
_ECCC_ID = re.compile(r"[a-z]{2}-[a-z0-9]{1,4}")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

class Fallback(NamedTuple):
    header: str
    rows: list[tuple[str, str]]  # (iso date, dated line)
    hours: dict[str, str] | None = None  # local "YYYY-MM-DDTHH:00" -> line; ECCC only


def _en(value: Any) -> str:
    """ECCC wraps every string as {"en": ..., "fr": ...}."""
    if isinstance(value, dict):
        value = value.get("en")
    return _text(value if isinstance(value, str) else None, 600) or ""


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def _stamp(moment: datetime) -> str:
    return f"{moment:%a %Y-%m-%d %H:%M} {moment.tzname()}"


# One server process lives for one `claude -p` call, so a plain dict is the
# right lifetime: the fallback and the alerts line ask for the same page, and a
# route's neighbouring stops often share one.
_ECCC_PAGES: dict[tuple[float, float, float], tuple[float, str, dict] | None] = {}


def _eccc_page(lat: float, lon: float, radius: float = ECCC_RADIUS_KM, *,
               quick: bool = False) -> tuple[float, str, dict] | None:
    """(distance km, id, properties) of Environment Canada's nearest city
    page within `radius`, or None when there is none."""
    key = (lat, lon, radius)
    if key in _ECCC_PAGES:
        return _ECCC_PAGES[key]
    # One listing at the widest radius serves both: the fallback asks at 40 km
    # and then, past the NWS, at 100.
    nearest = _eccc_nearest(lat, lon, ECCC_FAR_KM, quick=quick)
    found = None
    if nearest is not None and nearest[0] <= radius:
        found = (nearest[0], nearest[1], _fetch_json(f"{ECCC_URL}/{nearest[1]}?f=json", quick=quick)["properties"])
    _ECCC_PAGES[key] = found
    return found


_ECCC_NEAREST: dict[tuple[float, float, float], tuple[float, str] | None] = {}


def _eccc_nearest(lat: float, lon: float, radius: float, *, quick: bool = False) -> tuple[float, str] | None:
    """(distance km, id) of the nearest city page within `radius`, from the
    listing alone. Also how the alerts line knows a point is in Canada."""
    key = (lat, lon, radius)
    if key in _ECCC_NEAREST:
        return _ECCC_NEAREST[key]
    # Wide enough to hold the whole radius: a degree of longitude shrinks
    # toward the pole, so a fixed width missed northern pages in review.
    dlat = radius / 111 + 0.05
    dlon = min(radius / (111 * max(math.cos(math.radians(lat)), 0.05)) + 0.05, 20)
    bbox = f"{lon - dlon:.2f},{lat - dlat:.2f},{lon + dlon:.2f},{lat + dlat:.2f}"
    listing = _fetch_json(f"{ECCC_URL}?f=json&properties=identifier&limit=500&bbox={bbox}", quick=quick)
    if not isinstance(listing, dict) or not isinstance(listing.get("features"), list):
        # Read as "no city page here", which would quietly turn Canada into
        # nowhere: say so.
        _drift("eccc city pages", f"listing has no features list near {_where(lat, lon)}")
        listing = {"features": []}
    nearest = None
    for feature in listing["features"]:
        coords = (feature.get("geometry") or {}).get("coordinates") or []
        ident = feature.get("id")
        # The id goes into a URL path, so it must look like one before it does.
        if len(coords) < 2 or not isinstance(ident, str) or not _ECCC_ID.fullmatch(ident):
            _drift("eccc city pages", f"skipped a listing entry: id {str(ident)[:40]!r}, {len(coords)} coordinates")
            continue
        distance = _km(lat, lon, coords[1], coords[0])
        if nearest is None or distance < nearest[0]:
            nearest = (distance, ident)
    found = nearest if nearest is not None and nearest[0] <= radius else None
    _ECCC_NEAREST[key] = found
    return found


def _eccc_hour(entry: dict, tz: Any) -> tuple[str, str]:
    """One of a city page's hourly forecasts: (local "YYYY-MM-DDTHH:00", line).
    Unlike Open-Meteo's, each is a forecast for that hour, not the hour before."""
    moment = _utc(entry["timestamp"]).astimezone(tz)
    wind = entry.get("wind") or {}
    value = lambda block: (block or {}).get("value", {}).get("en") if isinstance(block, dict) else None  # noqa: E731
    gust = value(wind.get("gust"))
    return f"{moment:%Y-%m-%dT%H}:00", (
        f"{moment:%H:%M} {moment.tzname()}: {_fmt(value(entry.get('temperature')))}°C, "
        f"{_en(entry.get('condition')) or DASH}, {_fmt(value(entry.get('lop')), '%')} chance, "
        f"wind {_text(value(wind.get('direction')), 4) or ''} {_fmt(value(wind.get('speed')))}"
        f"{' gusting ' + _fmt(gust) if gust is not None else ''} km/h [Environment Canada]"
    )


def _eccc(lat: float, lon: float, radius: float = ECCC_RADIUS_KM) -> Fallback | None:
    """Environment Canada's nearest city page as a forecast, or None outside
    Canada. Alerts are `_alerts`' business, from the alert polygons."""
    page = _eccc_page(lat, lon, radius)
    if page is None:
        return None
    distance, ident, props = page
    tz = ZoneInfo(_canadian_zone(ident[:2], lat, lon))
    group = props["forecastGroup"]
    issued = _utc(_en(group["timestamp"])).astimezone(tz)

    # Periods are named, not dated ("Tuesday", "Tuesday night"). Anchor the
    # first name to the issue date (or the day either side of it), then walk.
    rows: list[tuple[str, str]] = []
    day = None
    for period in group.get("forecasts") or []:
        label = _en((period.get("period") or {}).get("value"))
        name = label.split(" ")[0].capitalize()
        if name not in WEEKDAYS:
            continue
        if day is None:
            day = next((issued.date() + timedelta(days=k) for k in (0, -1, 1)
                        if WEEKDAYS[(issued.date() + timedelta(days=k)).weekday()] == name), issued.date())
        while WEEKDAYS[day.weekday()] != name:
            day += timedelta(days=1)
        half = "night" if "night" in label.lower() else "day"
        rows.append((day.isoformat(), f"{_weekday(day.isoformat())} {half}: {_en(period.get('textSummary'))}"))

    if not rows:
        _drift("eccc city page", f"{ident}: no dated periods in {len(group.get('forecasts') or [])} forecasts")
    hours = {}
    for entry in (props.get("hourlyForecastGroup") or {}).get("hourlyForecasts") or []:
        try:
            stamp, line = _eccc_hour(entry, tz)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        hours[stamp] = line
    if not hours:
        _drift("eccc city page", f"{ident}: no hourly forecasts")
    # The region says what the page covers: Valemount's and McBride's pages are
    # one forecast, "Yellowhead", pinned at the same spot.
    region = _en(props.get("region"))
    where = (f"nearest city page, {distance:.0f} km away" if distance <= ECCC_RADIUS_KM else
             f"nearest city page is {distance:.0f} km away, so a regional guide for this point, not a local forecast")
    header = (f"Environment Canada forecast for {_en(props.get('name'))} "
              f"({region + '; ' if region else ''}{where}), issued {_stamp(issued)}.")
    return Fallback(header, rows, hours)


def _in_uk(lat: float, lon: float) -> bool:
    """A box around Great Britain and Ireland (same clock all year), minus the
    corner of northern France the box would otherwise catch (Dieppe, Boulogne)."""
    return 49.8 <= lat <= 60.9 and -10.7 <= lon <= 1.8 and not (lat < 51.0 and lon > 0.9)


def _metno_sky(symbol: str | None) -> str:
    """"lightrainshowersandthunder_day" -> "light rain showers and thunder"."""
    if not symbol:
        return DASH
    words = re.sub(r"_(day|night|polartwilight)$", "", symbol)
    return re.sub(r"(clear|partly|light|heavy|rain|sleet|snow|showers|and)(?=[a-z])", r"\1 ", words)


def _metno(lat: float, lon: float) -> Fallback:
    """MET Norway, summarised per UK day and night, the periods every other
    answer uses. Its timeseries is UTC, hourly for ~2.5 days then 6-hourly; a
    6-hourly step's precipitation is in `next_6_hours` and an hourly step's in
    `next_1_hours`, never both counted. A step belongs to the period it starts
    in, so a 6-hourly block's rain is credited there whole."""
    tz = ZoneInfo("Europe/London")
    payload = _fetch_json(f"{METNO_URL}?lat={lat:.2f}&lon={lon:.2f}")
    props = payload["properties"]
    periods: dict[tuple[str, bool], dict] = {}
    for step in props.get("timeseries") or []:
        moment = _utc(step["time"]).astimezone(tz)
        night = not DAY_START <= moment.hour < NIGHT_START
        day = moment.date() - timedelta(days=1) if moment.hour < DAY_START else moment.date()
        data = step.get("data") or {}
        now = (data.get("instant") or {}).get("details") or {}
        bucket = periods.setdefault((day.isoformat(), night),
                                    {"temps": [], "rain": None, "wind": (None, None), "sky": (99, None),
                                     "first": moment})
        if now.get("air_temperature") is not None:
            bucket["temps"].append(now["air_temperature"])
        speed = now.get("wind_speed")
        if speed is not None and (bucket["wind"][0] is None or speed > bucket["wind"][0]):
            bucket["wind"] = (speed, now.get("wind_from_direction"))
        block = data.get("next_1_hours") or data.get("next_6_hours") or {}
        amount = (block.get("details") or {}).get("precipitation_amount")
        if amount is not None:  # no block is "unknown", never "dry"
            bucket["rain"] = (bucket["rain"] or 0.0) + amount
        # The symbol nearest the middle of the period: 12:00, or 00:00.
        middle = abs(moment.hour - 12) if not night else min(moment.hour, 24 - moment.hour)
        symbol = ((data.get("next_6_hours") or data.get("next_1_hours") or {}).get("summary") or {}).get("symbol_code")
        if symbol and middle < bucket["sky"][0]:
            bucket["sky"] = (middle, symbol)
    rows = []
    for (iso, night), b in periods.items():
        # The series ends with a lone step: one sample is not a period (a
        # lone midnight step rendered as "9–9°C, 0.0 mm" in review,
        # 2026-10-13). The first period starts at the current hour, so it
        # says where it starts rather than passing an afternoon low off as
        # the day's.
        if len(b["temps"]) < 2:
            continue
        opens = NIGHT_START if night else DAY_START
        since = f" (from {b['first']:%H:%M})" if (b["first"].hour - opens) % 24 > 1 else ""
        speed, bearing = b["wind"]
        rows.append((iso, f"{_weekday(iso)} {'night' if night else 'day'}{since}: "
                          f"{min(b['temps']):.0f}–{max(b['temps']):.0f}°C, {_metno_sky(b['sky'][1])}, "
                          f"{_fmt(b['rain'], ' mm', 1)}, wind {_compass(bearing)}{_fmt(speed and speed * 3.6)} km/h"))
    if not rows:
        _drift("met.no", f"no periods from {len(props.get('timeseries') or [])} timeseries steps")
    updated = _utc(props["meta"]["updated_at"]).astimezone(tz)
    return Fallback(f"MET Norway forecast (UK fallback), updated {_stamp(updated)}.", rows)


def _nws(lat: float, lon: float) -> Fallback | None:
    """The US National Weather Service, or None where it has no grid (404)."""
    try:
        point = _fetch_json(f"{NWS_URL}/{lat:.4f},{lon:.4f}")["properties"]
    except _HTTPStatus as e:
        if e.code == 404:
            return None
        raise
    url = point.get("forecast") or ""
    # Follow only NWS's own forecast link, never wherever a response points.
    if not url.startswith("https://api.weather.gov/gridpoints/"):
        _drift("nws", f"forecast link {_quiet_url(url) if url else 'missing'}")
        raise ToolError("the NWS returned an unexpected forecast link")
    props = _fetch_json(f"{url}?units=si")["properties"]
    try:
        tz = ZoneInfo(point.get("timeZone") or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        tz = timezone.utc
    rows = []
    for period in props.get("periods") or []:
        start = period.get("startTime") or ""
        temp = period.get("temperature")
        if period.get("temperatureUnit") == "F" and isinstance(temp, (int, float)):
            temp = (temp - 32) * 5 / 9
        pop = (period.get("probabilityOfPrecipitation") or {}).get("value")
        daytime = bool(period.get("isDaytime"))
        rows.append((start[:10], (
            f"{_weekday(start[:10])} {'day' if daytime else 'night'}: "
            f"{'high' if daytime else 'low'} {_fmt(temp)}°C, {_text(period.get('shortForecast'), 80) or DASH}, "
            f"{_fmt(pop, '%')} chance of precipitation, wind {_text(period.get('windDirection'), 4) or ''} "
            f"{_text(period.get('windSpeed'), 30) or DASH}"
        )))
    if not rows:
        _drift("nws", f"no periods in the gridpoint forecast for {_where(lat, lon)}")
    where = (point.get("relativeLocation") or {}).get("properties") or {}
    near = ", ".join(x for x in (_text(where.get("city"), 60), _text(where.get("state"), 4)) if x)
    updated = props.get("updateTime") or props.get("generatedAt")
    when = f", updated {_stamp(_utc(updated).astimezone(tz))}" if updated else ""
    return Fallback(f"US National Weather Service forecast{' near ' + near if near else ''}{when}.", rows)


# --- alerts --------------------------------------------------------------------
#
# Added 2026-10-13 after first use: for a high-roof van on Hwy 17 in October, a
# wind warning or snow squalls matter more than any model's numbers, and Marlow
# could not check whether one was in effect.
#
# Canada: the alerts THEMSELVES, as Environment Canada publishes them -- one
# polygon per warning area -- tested against the exact point. The first version
# read the warnings on the nearest city page instead, and Marlow caught what
# that costs: Chutes got Gore Bay's page, 44 km away across the water on
# Manitoulin, a different warning area (2026-10-13). The US asks NWS at the
# point. The UK has no keyless feed, so it says so.
#
# Every answer carries an alerts line, including "none in effect", because the
# absence of a line would not tell Marlow whether anyone looked; a failure
# prints "could not check", never "none". It costs that line, never the forecast.

ECCC_ALERTS_URL = "https://api.weather.gc.ca/collections/weather-alerts/items"
# Seen live: "issued", "continued"; "updated" is in ECCC's CAP vocabulary.
# Anything else still counts as in effect, and is logged as drift.
KNOWN_ECCC_STATUSES = ("issued", "continued", "updated")
NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"

# Past the forecast fallback's 40 km, a point can still be Canadian: the camp
# at Agawa Bay is ~50 km from both Wawa and the Soo (review, 2026-10-13). Only
# decides that "none in effect" may be said; the alerts come from the polygons.
ALERT_RADIUS_KM = 150


def _in_us_box(lat: float, lon: float) -> bool:
    """Roughly the US and Alaska/Hawaii: only decides whether NWS is worth
    asking. Canada is settled before this is reached."""
    return 18.0 <= lat <= 72.0 and -180.0 <= lon <= -64.0


def _in_ring(lat: float, lon: float, ring: list) -> bool:
    """Ray casting; GeoJSON rings are [lon, lat]."""
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _contains(geometry: dict, lat: float, lon: float) -> bool:
    kind, coords = geometry.get("type"), geometry.get("coordinates") or []
    polygons = [coords] if kind == "Polygon" else coords if kind == "MultiPolygon" else []
    return any(
        rings and _in_ring(lat, lon, rings[0]) and not any(_in_ring(lat, lon, hole) for hole in rings[1:])
        for rings in polygons
    )


def _eccc_alerts(lat: float, lon: float) -> list[str]:
    """Active Environment Canada alerts whose area contains the point."""
    bbox = f"{lon - 0.01:.4f},{lat - 0.01:.4f},{lon + 0.01:.4f},{lat + 0.01:.4f}"
    payload = _fetch_json(f"{ECCC_ALERTS_URL}?f=json&lang=en&limit=100&bbox={bbox}", quick=True)
    # Every skip below would otherwise end as "none in effect", so each one
    # that is not the ordinary kind (an area beside the point, an ended alert)
    # is logged as drift.
    if not isinstance(payload, dict) or not isinstance(payload.get("features"), list):
        _drift("eccc alerts", "response has no features list")
        raise ToolError("Environment Canada's alert feed returned an unexpected shape")
    now = _now()
    found = []
    for feature in payload["features"]:
        props = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        if geometry.get("type") not in ("Polygon", "MultiPolygon"):
            _drift("eccc alerts", f"alert area of type {str(geometry.get('type'))[:30]!r}, not a polygon")
            continue
        if not _contains(geometry, lat, lon):
            continue
        # Ended and cancelled alerts linger in the feed for a while.
        status = (_text(props.get("status_en"), 20) or "").lower()
        if status in ("ended", "cancelled", "canceled"):
            continue
        if status not in KNOWN_ECCC_STATUSES:
            _drift("eccc alerts", f"unrecognised status_en {status!r} (counted as in effect)")
        name = _text(props.get("alert_name_en"), 80)
        if not name:
            _drift("eccc alerts", f"an alert at {_where(lat, lon)} has no alert_name_en; keys: "
                                  f"{','.join(sorted(props))[:300]}")
            name = "unnamed alert (feed format changed)"
        expires = props.get("expiration_datetime")
        if isinstance(expires, str) and _utc(expires) < now:
            continue
        area = _text(props.get("feature_name_en"), 80)
        until = f", until {_stamp(_utc(expires).astimezone(_province_tz(props, lat, lon)))}" if isinstance(expires, str) else ""
        summary = _text(str(props.get("alert_text_en") or "").split("\n")[0], 120)
        line = f"{name.capitalize()}{f' for {area}' if area else ''}{until}{f' ({summary})' if summary else ''}"
        if line not in found:
            found.append(line)
    return found


def _province_tz(props: dict, lat: float, lon: float) -> Any:
    province = (_text(props.get("province"), 4) or "on").lower()
    return ZoneInfo(_canadian_zone(province, lat, lon))


def _nws_covers(lat: float, lon: float) -> bool:
    """NWS has a grid here (its /points 404s anywhere outside the US)."""
    try:
        _fetch_json(f"{NWS_URL}/{lat:.4f},{lon:.4f}", quick=True)
    except _HTTPStatus as e:
        if e.code == 404:
            return False
        raise
    return True


def _nws_alerts(lat: float, lon: float) -> list[str]:
    payload = _fetch_json(f"{NWS_ALERTS_URL}?point={lat:.4f},{lon:.4f}", quick=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("features"), list):
        _drift("nws alerts", "response has no features list")
        raise ToolError("the NWS alert feed returned an unexpected shape")
    found = []
    for feature in payload["features"]:
        props = feature.get("properties") or {}
        event = _text(props.get("event"), 80)
        if not event:
            _drift("nws alerts", f"an alert has no event; keys: {','.join(sorted(props))[:300]}")
            event = "unnamed alert (feed format changed)"
        severity = _text(props.get("severity"), 20)
        ends = props.get("ends") or props.get("expires")
        until = ""
        if isinstance(ends, str):
            try:
                moment = datetime.fromisoformat(ends)
                until = f", until {moment:%a %Y-%m-%d %H:%M} (UTC{moment:%z})"
            except ValueError:
                pass
        line = f"{event}{f' ({severity.lower()})' if severity else ''}{until}"
        if line not in found:
            found.append(line)
    return found


def _alerts(lat: float, lon: float) -> str:
    """One line: what is in effect, that nothing is, or that nobody could say.

    Never "none in effect" from a source that does not cover the point: a
    remote Canadian camp once fell through to NWS, which has nothing for
    Canada, and was told all was clear (review, 2026-10-13)."""
    try:
        found = _eccc_alerts(lat, lon)
        if found:  # an Environment Canada area contains the point: Canada
            return "ALERTS IN EFFECT (Environment Canada, at this point): " + "; ".join(found) + "."
        if _in_uk(lat, lon):
            return "Alerts: not checked (no keyless UK warnings feed)."
        # NWS before the city-page listing: Detroit is within 40 km of
        # Windsor's page, and was told "none (Environment Canada)" without NWS
        # being asked (live check, 2026-10-13). NWS 404s on Canadian points,
        # Windsor and the Soo included, so it settles which side a point is on.
        if _in_us_box(lat, lon) and _nws_covers(lat, lon):
            found = _nws_alerts(lat, lon)
            if found:
                return "ALERTS IN EFFECT (US National Weather Service): " + "; ".join(found) + "."
            return "Alerts: none in effect (US National Weather Service)."
        if _eccc_nearest(lat, lon, ALERT_RADIUS_KM, quick=True) is not None:
            return "Alerts: none in effect (Environment Canada, at this point)."
        return "Alerts: not checked (no alert source for this region)."
    except ToolError as e:
        return f"Alerts: could not check ({e})."
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        log.warning("SHAPE alerts at %s: %s: %s", _where(lat, lon), type(e).__name__, e, exc_info=True)
        return f"Alerts: could not check (unexpected shape, {type(e).__name__})."


def _alerts_for(points: list[tuple[float, float]]) -> dict[tuple[float, float], str]:
    """Each distinct point's alerts line, fetched concurrently, within the
    budget: one work queue serializes every claude -p call, so a slow tool
    holds up all of Marlow, and 12 stops are up to 24 requests (review:
    serially they added 10-20 s to every route)."""
    distinct = list(dict.fromkeys(points))
    pool = ThreadPoolExecutor(max_workers=ALERT_WORKERS)
    futures = {key: pool.submit(_alerts, *key) for key in distinct}
    wait(futures.values(), timeout=ALERT_BUDGET_SECONDS)
    pool.shutdown(wait=False, cancel_futures=True)
    return {key: f.result() if f.done() and not f.cancelled() else "Alerts: not checked (time budget spent)."
            for key, f in futures.items()}


def _fallback(lat: float, lon: float) -> Fallback:
    try:
        found = _eccc(lat, lon)
        if found is None and _in_uk(lat, lon):
            found = _metno(lat, lon)
        if found is None:
            found = _nws(lat, lon)
        if found is None:
            found = _eccc(lat, lon, ECCC_FAR_KM)
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        log.warning("SHAPE fallback at %s: %s: %s", _where(lat, lon), type(e).__name__, e, exc_info=True)
        raise
    if found is None:
        raise ToolError("no fallback covers this point (Canada, the UK and the US have one)")
    log.warning("FALLBACK used at %s: %s", _where(lat, lon), found[0])
    return found


def _fallback_or_error(lat: float, lon: float, primary: ToolError) -> Fallback:
    try:
        return _fallback(lat, lon)
    except ToolError as e:
        raise ToolError(f"Open-Meteo unavailable ({primary}); fallback failed too: {e}. "
                        "Tell the user the forecast is unavailable; do not search for one.") from None
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise ToolError(f"Open-Meteo unavailable ({primary}); fallback returned an unexpected shape "
                        f"({type(e).__name__}). Tell the user the forecast is unavailable; do not search "
                        "for one.") from None


def _fallback_hours(by_hour: dict[str, str] | None, hours: str) -> list[str]:
    """`hours` from a fallback: ECCC's next 24 hours, so some of tomorrow."""
    if not by_hour:
        return ["Hour-by-hour detail is not available from the fallback source."]
    first = min(by_hour)
    wanted = (date.fromisoformat(first[:10]) + timedelta(days=0 if hours == "today" else 1)).isoformat()
    # The first entry is the page's next hour, so its date is today's.
    picked = [line for stamp, line in sorted(by_hour.items()) if stamp.startswith(wanted)]
    if not picked:
        return [f"Hour-by-hour detail from the fallback ends {_weekday(max(by_hour)[:10])} {max(by_hour)[11:16]}."]
    ends = "" if hours == "today" or max(by_hour)[11:13] == "23" else f" (the fallback's hourly data ends {max(by_hour)[11:16]})"
    return [f"Hour by hour, {_weekday(wanted)}{ends}:", *picked]


def _rows_for(rows: list[tuple[str, str]], wanted: date) -> str:
    """One stop's lines for its date, or why there are none."""
    hits = [line for iso, line in rows if iso == wanted.isoformat()]
    if hits:
        return "; ".join(hits)
    if not rows:
        return "no forecast periods returned"
    first, last = rows[0][0], rows[-1][0]
    if wanted.isoformat() < first:
        return f"{wanted.isoformat()} is already past (forecast starts {first})"
    return f"{wanted.isoformat()} is beyond this source's forecast (it ends {last}; {BEYOND})"


# --- places along a route ------------------------------------------------------
#
# Town names for a route come from a file in the repo, not a service
# (docs/tasks/route-planning.md): deployed like code, no new egress. Built by
# scripts/build_places.py from GeoNames (CC BY 4.0, server/data/NOTICE).
# Canada and the UK are every settlement, because GeoNames lists most small
# places there at population 0 (Marathon ON, Tongue in Scotland); the US and
# Mexico are places of 1,000+. Outside those four countries nothing is named.

PLACES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "places.tsv.gz")
_CELL_DEG = 0.1
_KM_PER_DEG = 111.2


class _Place(NamedTuple):
    name: str
    lat: float
    lon: float
    country: str
    region: str
    population: int
    tz: str

    @property
    def label(self) -> str:
        return f"{self.name}, {self.region}" if self.region else self.name


class _PlaceIndex:
    """Places in a grid of 0.1° cells (~11 km): a query reads the few cells its
    radius touches, not all ~79K places."""

    def __init__(self) -> None:
        self._cells: dict[tuple[int, int], list[_Place]] = {}
        self._count = 0

    def __len__(self) -> int:
        return self._count

    @staticmethod
    def _cell(lat: float, lon: float) -> tuple[int, int]:
        return math.floor(lat / _CELL_DEG), math.floor(lon / _CELL_DEG)

    def add(self, place: _Place) -> None:
        key = (math.floor(place.lat / _CELL_DEG), math.floor(place.lon / _CELL_DEG))
        self._cells.setdefault(key, []).append(place)
        self._count += 1

    def near(self, lat: float, lon: float, radius_km: float) -> list[tuple[float, _Place]]:
        """(km, place) within radius_km, nearest first."""
        dlat = radius_km / _KM_PER_DEG
        # A degree of longitude shrinks with cos(lat), so the same radius spans
        # more cells east-west the further north it is. Size it at the edge of
        # the band nearest the pole, where it is widest; the floor keeps a
        # query at the pole itself finite.
        edge = min(abs(lat) + dlat, 89.0)
        dlon = dlat / max(math.cos(math.radians(edge)), 0.01)
        lo_i, lo_j = self._cell(lat - dlat, lon - dlon)
        hi_i, hi_j = self._cell(lat + dlat, lon + dlon)
        hits = []
        for i in range(lo_i, hi_i + 1):
            for j in range(lo_j, hi_j + 1):
                for place in self._cells.get((i, j), ()):
                    km = _km(lat, lon, place.lat, place.lon)
                    if km <= radius_km:
                        hits.append((km, place))
        hits.sort(key=lambda hit: hit[0])
        return hits

    def nearest(self, lat: float, lon: float, max_km: float) -> _Place | None:
        hits = self.near(lat, lon, max_km)
        return hits[0][1] if hits else None


_PLACES: _PlaceIndex | None = None


def _load_places() -> _PlaceIndex:
    index = _PlaceIndex()
    bad = 0
    try:
        with gzip.open(PLACES_PATH, "rb") as fh:
            text = fh.read().decode("utf-8", errors="replace")
        for line in text.split("\n"):
            if line:
                fields = line.rstrip("\r").split("\t")
                try:
                    if len(fields) != 7:
                        raise ValueError(line)
                    name, lat, lon, country, region, population, tz = fields
                    index.add(_Place(name, float(lat), float(lon), country, region, int(population), tz))
                except ValueError:
                    bad += 1
    except (OSError, EOFError, zlib.error) as exc:
        # Missing, unreadable or cut off. A forecast without town names is
        # still a forecast, so keep what loaded.
        log.warning("PLACES unavailable: %s: %s", type(exc).__name__, exc)
    if bad:
        _drift("places", f"{bad} malformed line(s) skipped in {os.path.basename(PLACES_PATH)}")
    return index


def _places() -> _PlaceIndex:
    """Loaded once per process (~100 ms), so the first route pays for it."""
    global _PLACES
    if _PLACES is None:
        _PLACES = _load_places()
    return _PLACES


def _decode_polyline(encoded: str, precision: int = 6) -> list[tuple[float, float]]:
    """Google's encoded polyline (OSRM `polyline6` is precision 6) as (lat, lon)."""
    factor = 10 ** precision
    points: list[tuple[float, float]] = []
    index, lat, lon = 0, 0, 0
    values = []
    while index < len(encoded):
        result, shift = 0, 0
        while True:
            if index >= len(encoded):
                raise ValueError("truncated polyline")
            chunk = ord(encoded[index]) - 63
            index += 1
            if not 0 <= chunk < 64:
                raise ValueError("bad character in polyline")
            result |= (chunk & 0x1F) << shift
            shift += 5
            if chunk < 0x20:
                break
        values.append(~(result >> 1) if result & 1 else result >> 1)
        if len(values) == 2:
            lat += values[0]
            lon += values[1]
            points.append((lat / factor, lon / factor))
            values = []
    if values:
        raise ValueError("truncated polyline (latitude without longitude)")
    return points


# --- routing: `depart` times the trip (docs/tasks/route-planning.md) ----------------
#
# Without it Marlow supplied coordinates and times for ~40 stops from its own
# knowledge, and the Nipigon stop landed in the lake (2026-10-13). So the tool
# does the arithmetic and geography -- road times, days, towns, zones -- and
# Marlow keeps the judgment: which route, where to sleep. To move an
# overnight it calls again with its choice as a stop, so nothing to chain.

OSRM_URL = "https://router.project-osrm.org/route/v1/driving/"
OSRM_QUERY = "overview=full&geometries=polyline6&steps=true&annotations=duration,distance"
# Mike, 2026-10-14: 08:00 "is a push". It varies a lot by person (some start
# at 05:00), so every day line says what it assumed and how to change it.
DEFAULT_START_HOUR = 10
DEFAULT_MAX_DRIVE_HOURS = 8
# A town this close to the road is one you pass through. A city's point is
# its centre, which a highway can bypass: the QE2 passes ~5 km from Red Deer's
# (100K people), and at 3 km Calgary → Edmonton named Blackfalds instead.
TOWN_KM = ((100_000, 10.0), (10_000, 6.0), (0, 3.0))
SAMPLE_KM = 1.0     # road sampled this often for towns (measured: ~50 ms for Cabo)
SNAP_FLAG_KM = 2.0  # a stop further than this from a road gets a word
BORDER_TOWN_KM = 30.0
BORDER_HOLD_KM = 5.0
GAP_HOURS = 2.0     # central Baja has 7.3 h with no town on record
OVER_CAP_HOURS = 2.0
# A suggested overnight's alternatives: the largest towns this far before the
# cap. Marathon → Calgary at 6.5 h suggested Sunshine, a hamlet, with Thunder
# Bay 1.8 h before the cap (2026-10-14). Marlow, not a rule, picks among them.
OPTIONS_HOURS = 2.0
OPTIONS_MAX = 3
# OSRM's demo server asks for at most one request a second.
OSRM_MIN_INTERVAL = 1.0
MIN_DAY_SECONDS = 15 * 60
MIN_REST_HOURS = 8
MIN_VIA_GAP_SECONDS = 30 * 60  # Blackfalds 10:55, Lacombe 11:03 is one stretch
MAX_DRIVING_DAYS = 21
BRIEF_POINTS_MAX = 12
# A trip of one driving day leaving this soon gets weather along the road,
# not just at its ends: a storm warning halfway is the reason to postpone.
BRIEF_WITHIN_HOURS = 24
ROUTER_DOWN = ("Call route_forecast again without depart, with a date (and hour) on each stop from "
               "your own estimate of the drive, and tell the user those times are estimates.")
# Compared with Google Maps on three legs (Mike, 2026-10-14): 1.33, 1.33, 1.34.
OSRM_NOTE = ("Times are OSRM's: about a third longer than Google Maps', so roughly time on the road "
             "with short breaks. Ferry crossings are included, waits and schedules are not.")


class _RouteStop(NamedTuple):
    lat: float
    lon: float
    label: str | None
    overnight: bool
    start_hour: int | None


class _Track(NamedTuple):
    coords: list[tuple[float, float]]
    secs: list[float]      # driving seconds from the start, at each coordinate
    km: list[float]
    stop_at: list[int]     # each stop's coordinate index
    snapped_km: list[float]
    ferries: list[tuple[int, int, str]]


class _Day(NamedTuple):
    start_s: float
    end_s: float
    start_at: datetime     # aware, the start's own zone
    end_at: datetime       # aware, the end's own zone
    start_label: str
    end_label: str
    end_point: tuple[float, float]
    end_kind: str          # "stop", "suggested", "over", "road"


def _route_shape(what: str) -> ToolError:
    _drift("osrm", what)
    return ToolError(f"Routing unavailable (unexpected response shape). {ROUTER_DOWN}")


_OSRM_LAST: list[float] = []


def _osrm(stops: list[_RouteStop]) -> Any:
    path = ";".join(f"{s.lon:g},{s.lat:g}" for s in stops)
    if _OSRM_LAST and (wait := OSRM_MIN_INTERVAL - (_clock() - _OSRM_LAST[0])) > 0:
        _sleep(wait)
    _OSRM_LAST[:] = [_clock()]
    try:
        payload = _fetch_json(f"{OSRM_URL}{path}?{OSRM_QUERY}")
    except ToolError as e:
        # A 400 here is OSRM's "NoRoute" (an ocean between stops) as often as
        # our own bug; either way the dated mode still answers the weather.
        raise ToolError(f"Routing unavailable ({e}). {ROUTER_DOWN}") from None
    if not isinstance(payload, dict) or payload.get("code") != "Ok" or not payload.get("routes"):
        code = payload.get("code") if isinstance(payload, dict) else type(payload).__name__
        raise _route_shape(f"code {code!r}")
    return payload


def _track(payload: dict, stops: int) -> _Track:
    route = payload["routes"][0]
    coords = _decode_polyline(route["geometry"])
    legs = route["legs"]
    if len(legs) != stops - 1:
        raise _route_shape(f"{len(legs)} legs for {stops} stops")
    secs, km, stop_at, ferries = [0.0], [0.0], [0], []
    for leg in legs:
        start = len(secs) - 1
        durations, distances = leg["annotation"]["duration"], leg["annotation"]["distance"]
        # Turn penalties are in a leg's duration but not its segments (2-3%
        # short on the recorded trips): stretch the segments to the leg.
        scale = leg["duration"] / sum(durations) if sum(durations) > 0 else 1.0
        for seconds, metres in zip(durations, distances):
            secs.append(secs[-1] + seconds * scale)
            km.append(km[-1] + metres / 1000)
        stop_at.append(len(secs) - 1)
        travelled = km[start]
        for step in leg["steps"]:
            if step.get("mode") == "ferry":
                first = bisect.bisect_left(km, travelled, lo=start)
                last = bisect.bisect_left(km, travelled + step["distance"] / 1000, lo=first)
                ferries.append((first, min(last, len(km) - 1), _text(step.get("name"), 60) or "ferry"))
            travelled += step["distance"] / 1000
    if len(secs) != len(coords):
        raise _route_shape(f"{len(coords)} coordinates, {len(secs)} timed")
    snapped = [float(w.get("distance") or 0) / 1000 for w in payload.get("waypoints") or []]
    return _Track(coords, secs, km, stop_at, snapped, ferries)


def _samples(track: _Track):
    """(lat, lon, seconds, km) every SAMPLE_KM along the road."""
    last = -SAMPLE_KM
    for (lat, lon), at, km in zip(track.coords, track.secs, track.km):
        if km - last >= SAMPLE_KM:
            last = km
            yield lat, lon, at, km


def _towns_along(track: _Track) -> list[tuple[float, _Place]]:
    """Every town within TOWN_KM of the road, at the time of its nearest approach."""
    index, best = _places(), {}
    for lat, lon, at, _ in _samples(track):
        for distance, place in index.near(lat, lon, TOWN_KM[0][1]):
            if distance > next(radius for size, radius in TOWN_KM if place.population >= size):
                continue
            if place not in best or distance < best[place][0]:
                best[place] = (distance, at)
    return sorted(((at, place) for place, (_, at) in best.items()), key=lambda pair: pair[0])


def _within_reach(lat: float, lon: float) -> list[_Place]:
    """Towns this point is in or passes through, by TOWN_KM's size rule."""
    return [place for distance, place in _places().near(lat, lon, TOWN_KM[0][1])
            if distance <= next(radius for size, radius in TOWN_KM if place.population >= size)]


def _town_at(lat: float, lon: float) -> str:
    """A name for an unlabelled stop: the largest town it is in, since the
    nearest point in a city is a neighbourhood's (downtown Calgary was
    "Glencoe, AB")."""
    towns = _within_reach(lat, lon)
    return max(towns, key=lambda place: place.population).label if towns else f"{lat:g}, {lon:g}"


def _crossings(track: _Track) -> list[str]:
    """Where the nearest town changes country and stays changed. The ferry
    to Victoria passes Point Roberts, WA, and was two crossings without the
    hold (recorded route, 2026-10-14)."""
    runs: list[list] = []  # [country, first km, last km, first place, last place]
    for lat, lon, _, km in _samples(track):
        place = _places().nearest(lat, lon, BORDER_TOWN_KM)
        if place is None:
            continue
        if runs and runs[-1][0] == place.country:
            runs[-1][2], runs[-1][4] = km, place
        else:
            runs.append([place.country, km, km, place, place])
    # A run shorter than BORDER_HOLD_KM mid-route is the far bank, not a crossing.
    held = [run for n, run in enumerate(runs)
            if n in (0, len(runs) - 1) or run[2] - run[1] >= BORDER_HOLD_KM]
    merged: list[list] = []
    for run in held:
        if merged and merged[-1][0] == run[0]:
            merged[-1][2], merged[-1][4] = run[2], run[4]
        else:
            merged.append(list(run))
    return [f"{a[4].label} → {b[3].label}" for a, b in zip(merged, merged[1:])]


def _tz_at(lat: float, lon: float) -> Any:
    """The nearest town's zone; the weather call would give one too, but a
    trip past the forecast range makes none."""
    # Near first: 150 km is ~1,000 grid cells, and nearly every stop has a
    # town within 30.
    place = _places().nearest(lat, lon, 30) or _places().nearest(lat, lon, 150)
    return _zone_of(place.tz) if place else timezone.utc


def _zone_of(name: str) -> Any:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _local_time(at: datetime, tz: Any) -> str:
    local = at.astimezone(tz)
    return f"{local:%H:%M} {local.tzname()}"


def _on_the_hour(at: datetime) -> tuple[date, int]:
    """The nearest whole hour, which is what the hourly forecast has."""
    rounded = (at + timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0)
    return rounded.date(), rounded.hour


def _plan_days(track: _Track, stops: list[_RouteStop], labels: list[str],
               towns: list[tuple[float, _Place]], depart: datetime,
               start_hour: int, cap_hours: float) -> list[_Day]:
    ends = [(track.secs[track.stop_at[i]], i) for i in range(1, len(stops))
            if stops[i].overnight or i == len(stops) - 1]
    days: list[_Day] = []
    start_s, start_at, start_label, mark = 0.0, depart, labels[0], 0
    while True:
        if len(days) == MAX_DRIVING_DAYS:
            raise ToolError(f"more than {MAX_DRIVING_DAYS} days of driving at {cap_hours:g} h a day; "
                            "raise max_drive_hours or split the trip")
        cap = start_s + cap_hours * 3600
        # A marked overnight where the day starts (a repeated stop) ends nothing.
        while ends[mark][0] <= start_s and mark < len(ends) - 1:
            mark += 1
        stop_s, i = ends[mark]
        if stop_s <= cap:
            end_s, label, point, kind = stop_s, labels[i], (stops[i].lat, stops[i].lon), "stop"
            mark += 1
        else:
            before = [(s, p) for s, p in towns if start_s + MIN_DAY_SECONDS < s <= cap]
            after = [(s, p) for s, p in towns if cap < s <= min(cap + OVER_CAP_HOURS * 3600, stop_s)]
            # The largest town in the last hour before the cap, not simply the
            # last: the last before Salt Lake's cap was Vineyard, a suburb.
            late = [(s, p) for s, p in before if s > cap - 3600]
            if before or after:
                end_s, place = (max(late, key=lambda pair: pair[1].population) if late
                                else before[-1] if before else after[0])
                label, point, kind = place.label, (place.lat, place.lon), "suggested" if before else "over"
            else:
                at = min(bisect.bisect_left(track.secs, cap), len(track.secs) - 1)
                end_s, (lat, lon) = track.secs[at], track.coords[at]
                point = (round(lat, COORD_DECIMALS), round(lon, COORD_DECIMALS))
                label, kind = f"the road at {point[0]:g}, {point[1]:g}", "road"
        tz = _tz_at(*point)
        end_at = (start_at + timedelta(seconds=end_s - start_s)).astimezone(tz)
        days.append(_Day(start_s, end_s, start_at, end_at, start_label, label, point, kind))
        if kind == "stop" and i == len(stops) - 1:
            return days
        hour = stops[i].start_hour if kind == "stop" and stops[i].start_hour is not None else start_hour
        start_s, start_label = end_s, label
        start_at = _next_start(end_at, hour)


def _overnight_options(day: _Day, cap_s: float, towns: list[tuple[float, _Place]]) -> str | None:
    """The suggested overnight and the largest towns in the hours before the
    cap, half an hour apart so one city's suburbs don't fill the list, in road
    order: 'Thunder Bay, ON 4.7 h (109k) · Sunshine, ON 5.5 h (small)'. None
    when there is nothing to choose between."""
    window = [(at, p) for at, p in towns
              if max(day.start_s + MIN_DAY_SECONDS, cap_s - OPTIONS_HOURS * 3600) < at <= cap_s]
    picks = [next((pair for pair in window if pair[0] == day.end_s), (day.end_s, None))]
    for at, place in sorted(window, key=lambda pair: -pair[1].population):
        if len(picks) == OPTIONS_MAX or not place.population:
            break
        # A city beside a small pick still counts: Henderson's pick hid Las Vegas.
        near_ok = picks[0][1] is None or place.population > picks[0][1].population
        if all(abs(at - s) >= MIN_VIA_GAP_SECONDS or (n == 0 and near_ok) for n, (s, _) in enumerate(picks)):
            picks.append((at, place))
    if len(picks) < 2:
        return None
    return " · ".join(f"{place.label if place else day.end_label} {(at - day.start_s) / 3600:.1f} h "
                      f"({_size(place.population if place else 0)})"
                      for at, place in sorted(picks, key=lambda pair: pair[0]))


def _size(population: int) -> str:
    if population >= 10_000:
        return f"{population / 1000:.0f}k"
    if population >= 1000:
        return f"{population / 1000:.1f}k"
    # GeoNames has no count for most hamlets (every Canadian one is listed).
    return f"{population:,}" if population else "small"


def _next_start(arrived: datetime, hour: int) -> datetime:
    """The first `hour` o'clock a night's rest after arriving: arriving at
    02:00 leaves at 10:00 that day, not the next."""
    start = arrived.replace(hour=hour, minute=0, second=0, microsecond=0)
    while start < arrived + timedelta(hours=MIN_REST_HOURS):
        start = (start + timedelta(days=1)).replace(hour=hour)
    return start


def _hourly_towns(day: _Day, towns: list[tuple[float, _Place]]) -> list[tuple[float, _Place]]:
    """The largest town in each hour of the day's driving: one name an hour
    keeps Calgary → Cabo's routing to ~2K characters, and the largest is the
    one a person knows (the smallest are suburbs and ejidos)."""
    picks: dict[int, tuple[float, _Place]] = {}
    for at, place in towns:
        if day.start_s + MIN_DAY_SECONDS < at < day.end_s - 10 * 60:
            hour = int((at - day.start_s) // 3600)
            if hour not in picks or place.population > picks[hour][1].population:
                picks[hour] = (at, place)
    kept: list[tuple[float, _Place]] = []
    for at, place in sorted(picks.values(), key=lambda pair: pair[0]):
        if kept and at - kept[-1][0] < MIN_VIA_GAP_SECONDS:
            if place.population > kept[-1][1].population:
                kept[-1] = (at, place)
            continue
        kept.append((at, place))
    return kept


def _depart(args: dict) -> datetime:
    value = args.get("depart")
    try:
        # A bare date parses as midnight; a departure needs its hour.
        if isinstance(value, str) and len(value) >= 16:
            return datetime.fromisoformat(value)
    except ValueError:
        pass
    raise ToolError("depart must be a local date and time like 2026-10-15T10:00")


def _parse_route_stop(n: int, stop: Any) -> _RouteStop:
    if not isinstance(stop, dict):
        raise ToolError(f"stop {n} must be an object")
    try:
        _known(stop, TOOLS["route_forecast"][1]["inputSchema"]["properties"]["stops"]["items"])
        if "date" in stop or "hour" in stop:
            raise ToolError("date and hour are for a trip without depart; with depart the tool times "
                            "every stop itself")
        overnight = stop.get("overnight", False)
        if not isinstance(overnight, bool):
            raise ToolError("overnight must be true or false")
        start_hour = _int(stop, "start_hour", 0, 23)
        if start_hour is not None and not overnight:
            raise ToolError("start_hour belongs on an overnight stop (the next morning's start)")
        lat, lon = _coords(stop)
        return _RouteStop(lat, lon, _text(stop.get("label"), 60), overnight, start_hour)
    except ToolError as e:
        raise ToolError(f"stop {n}: {e}") from None


def _routed(args: dict, stops: list) -> str:
    if len(stops) < 2:
        raise ToolError("with depart, give at least two stops: where you leave from and where you're going")
    parsed = [_parse_route_stop(n, s) for n, s in enumerate(stops, 1)]
    depart = _depart(args)
    start_hour = _int(args, "start_hour", 0, 23, default=DEFAULT_START_HOUR)
    cap_hours = (_number(args, "max_drive_hours", 1, 16) if "max_drive_hours" in args
                 else float(DEFAULT_MAX_DRIVE_HOURS))
    payload = _osrm(parsed)
    try:
        track = _track(payload, len(parsed))
    except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError) as e:
        log.warning("SHAPE osrm: %s: %s", type(e).__name__, e, exc_info=True)
        raise ToolError(f"Routing unavailable (unexpected response shape, {type(e).__name__}). "
                        f"{ROUTER_DOWN}") from None
    towns = _towns_along(track)
    labels = [s.label or _town_at(s.lat, s.lon) for s in parsed]
    first_tz = _tz_at(parsed[0].lat, parsed[0].lon)
    depart = depart.astimezone(first_tz) if depart.tzinfo else depart.replace(tzinfo=first_tz)
    days = _plan_days(track, parsed, labels, towns, depart, start_hour, cap_hours)
    passed = [(track.secs[track.stop_at[i]], f"{labels[i]} (stop {i + 1})", (parsed[i].lat, parsed[i].lon))
              for i in range(1, len(parsed) - 1) if not parsed[i].overnight]

    start_said = "as asked" if "start_hour" in args else "default; pass start_hour"
    cap_said = "as asked" if "max_drive_hours" in args else "default; pass max_drive_hours"
    lines = [
        f"Route: {labels[0]} → {labels[-1]}, {track.km[-1]:,.0f} km, {track.secs[-1] / 3600:.1f} h of driving "
        f"over {len(days)} day{'s' if len(days) > 1 else ''}. {OSRM_NOTE}",
    ]
    if len(days) > 1:
        lines.append(
            f"Each day after an overnight starts at {start_hour:02d}:00 local ({start_said}; an overnight stop's "
            f"own start_hour overrides it) and ends by {cap_hours:g} h of driving ({cap_said}). A suggested overnight is the "
            "largest town in the last hour before that, and its options line lists the larger towns in the "
            f"{OPTIONS_HOURS:g} h before the cap; to choose another, call again with it as a stop with overnight: true.")
    crossings = _crossings(track)
    if crossings:
        lines.append("Border crossings: " + "; ".join(dict.fromkeys(crossings)) + ".")
    for first, last, name in track.ferries:
        ends = [p.label if (p := _places().nearest(*track.coords[i], 20)) else "?" for i in (first, last)]
        # OSRM's name is the map's, sometimes a berth ("Berth 2 Approach"),
        # so the towns at each end lead.
        lines.append(f"Ferry: {ends[0]} → {ends[1]}, {(track.secs[last] - track.secs[first]) / 3600:.1f} h "
                     f"crossing (OSRM: {name}).")
    for n, (stop, km) in enumerate(zip(parsed, track.snapped_km), 1):
        if km > SNAP_FLAG_KM:
            lines.append(f"Stop {n} ({labels[n - 1]}) is {km:.0f} km from the nearest road; its times are "
                         "for where the road gets closest, and its weather for the stop itself.")
    if not len(_places()):
        lines.append("Town names are unavailable (the places file did not load), so days end on the road.")
    if any(timezone.utc in (d.start_at.tzinfo, d.end_at.tzinfo) for d in days):
        lines.append("Times marked UTC are where no town is near enough to give the local zone (outside "
                     "Canada, the US, Mexico and the UK).")

    for n, day in enumerate(days, 1):
        hours, km = (day.end_s - day.start_s) / 3600, _km_between(track, day)
        note = {"suggested": " (suggested overnight)", "stop": "",
                "over": f" (over the {cap_hours:g} h cap: no town before it)",
                "road": " (no town near the cap: choose an overnight)"}[day.end_kind]
        lines.append(f"Day {n}, {day.start_at:%a %d %b}: {day.start_label} {_local_time(day.start_at, day.start_at.tzinfo)} → "
                     f"{day.end_label} {_local_time(day.end_at, day.end_at.tzinfo)}{note}, {km:,.0f} km, {hours:.1f} h")
        stops_today = [(at, label, _tz_at(*point)) for at, label, point in passed if day.start_s < at < day.end_s]
        # A town is not named again beside the stop it is.
        via = [(at, p.name, _zone_of(p.tz)) for at, p in _hourly_towns(day, towns)
               if all(abs(at - s) > 10 * 60 for s, _, _ in stops_today)] + stops_today
        if via:
            shown, zone = [], day.start_at.tzname()
            # The zone is named where it changes: Saint George 12:17 MDT then
            # Mesquite 12:00 reads as time running backwards without it.
            for at, name, tz in sorted(via, key=lambda entry: entry[0]):
                local = (day.start_at + timedelta(seconds=at - day.start_s)).astimezone(tz)
                mark = "" if local.tzname() == zone else f" {local.tzname()}"
                zone = local.tzname()
                shown.append(f"{local:%H:%M}{mark} {name}")
            lines.append("   via " + " · ".join(shown))
        options = (_overnight_options(day, day.start_s + cap_hours * 3600, towns)
                   if day.end_kind == "suggested" else None)
        if options:
            lines.append(f"   overnight options: {options}")
        named = [(day.start_s, day.start_label)] + [(at, p.label) for at, p in towns
                                                    if day.start_s < at < day.end_s] + [(day.end_s, day.end_label)]
        for (a, a_name), (b, b_name) in zip(named, named[1:]):
            if b - a > GAP_HOURS * 3600:
                lines.append(f"   Nothing named for {(b - a) / 3600:.1f} h after {a_name} (to {b_name}).")

    points = _route_points(track, parsed, labels, days, towns, depart)
    if points is None:
        lines.append(f"Weather: the departure is beyond the {MAX_DAYS}-day forecast; {BEYOND}.")
        return "\n".join(lines)
    lines.append("Weather at the start, each overnight, each stop and the destination, at the hour you get there"
                 + (", and at each town on the way:" if any(p.brief for p in points) else ":"))
    lines.append(_stops_weather(points))
    return "\n".join(lines)


def _km_between(track: _Track, day: _Day) -> float:
    first = bisect.bisect_left(track.secs, day.start_s)
    last = min(bisect.bisect_left(track.secs, day.end_s), len(track.km) - 1)
    return track.km[last] - track.km[first]


def _route_points(track: _Track, stops: list[_RouteStop], labels: list[str], days: list[_Day],
                  towns: list[tuple[float, _Place]], depart: datetime) -> list[_Stop] | None:
    """Where the weather is asked for, in the order you get there; None past
    the forecast's range, where there is nothing to ask."""
    if (depart.date() - _now().astimezone(depart.tzinfo).date()).days >= MAX_DAYS:
        return None
    timed: list[tuple[float, _Stop]] = []

    def add(day: _Day, at: float, lat: float, lon: float, label: str, brief: bool = False) -> None:
        # The stop's own zone: Open-Meteo answers each point in its local time.
        local = (day.start_at + timedelta(seconds=at - day.start_s)).astimezone(_tz_at(lat, lon))
        timed.append((at, _Stop(lat, lon, *_on_the_hour(local), label, brief, local)))

    for day in days:
        if day.start_s == 0:
            add(day, 0.0, stops[0].lat, stops[0].lon, labels[0])
        add(day, day.end_s, *day.end_point, day.end_label)
        for i in range(1, len(stops) - 1):
            at = track.secs[track.stop_at[i]]
            if not stops[i].overnight and day.start_s < at < day.end_s:
                add(day, at, stops[i].lat, stops[i].lon, labels[i])
    leaving = days[0].start_at - _now()
    if len(days) == 1 and timedelta(hours=-3) <= leaving <= timedelta(hours=BRIEF_WITHIN_HOURS):
        for at, place in _hourly_towns(days[0], towns)[:BRIEF_POINTS_MAX]:
            add(days[0], at, place.lat, place.lon, place.label, brief=True)
    return [stop for _, stop in sorted(timed, key=lambda pair: pair[0])]


# --- tools ---------------------------------------------------------------------

def geocode(args: dict) -> str:
    name = _text(args.get("name"), 100)
    if not name:
        raise ToolError("name is required")
    params = {"name": name, "count": str(GEOCODE_RESULTS), "language": "en", "format": "json"}
    country = args.get("country_code")
    if country is not None:
        if not (isinstance(country, str) and re.fullmatch(r"[A-Za-z]{2}", country)):
            raise ToolError("country_code must be two letters, like CA")
        params["countryCode"] = country.upper()
    payload = _fetch_json(f"{GEOCODE_URL}?{urllib.parse.urlencode(params)}")
    results = payload.get("results") if isinstance(payload, dict) else None
    if not results:
        return f"No place matched {name!r}."
    lines = [f"Places matching {name!r} (pick one; pass its latitude/longitude to forecast):"]
    for r in results:
        where = ", ".join(_text(r.get(k), 60) for k in ("name", "admin1", "country") if _text(r.get(k), 60))
        if _text(r.get("admin2"), 60):
            where += f" ({_text(r.get('admin2'), 60)})"
        population = f" · pop {r['population']:,}" if isinstance(r.get("population"), int) else ""
        lines.append(
            f"- {where}: {r.get('latitude', 0):.2f}, {r.get('longitude', 0):.2f}"
            f" · {_text(r.get('timezone'), 40) or 'UTC'}{population}"
        )
    return "\n".join(lines)


def _hours(args: dict) -> str | None:
    value = args.get("hours")
    if value is None or value in HOURS_CHOICES:
        return value
    raise ToolError("hours must be 'today' or 'tomorrow' (hour-by-hour detail goes no further)")


LEGEND = ("Day = 06:00–18:00, night = 18:00–06:00 the next morning, in local time; after day "
          f"{PERIOD_DAYS}, one line per day (worst = its most severe condition). Each condition says when. [GEM] = Environment "
          "Canada's model; [best match] = Open-Meteo's pick where GEM has no data.")


def _periods(point: dict, days: list[date], since: str, *, last_night: bool = True) -> list[str]:
    """Day and night lines for each of `days`, plus, before dawn, the rest of
    last night: at 02:00 "tonight" is the night that started yesterday. With
    no hourly data at all, the days' daily rows instead, and the drift logged:
    a stop with nothing under it would read as nothing to say."""
    hourly, tz, daily = point.get("hourly") or {}, _zone(point), point["daily"]
    wanted = [(d, night) for d in days for night in (False, True)]
    if last_night and days:
        wanted.insert(0, (days[0] - timedelta(days=1), True))
    lines = [line for d, night in wanted if (line := _period_line(hourly, tz, d, night, since, daily))]
    if days and not lines:
        _drift("open-meteo", f"no day/night periods for {_where(point.get('latitude', 0), point.get('longitude', 0))}; "
                             f"hourly keys: {','.join(sorted(hourly))[:300]}")
        times = daily.get("time") or []
        lines = [_daily_line(daily, times.index(d.isoformat())) for d in days if d.isoformat() in times]
    return lines


def forecast(args: dict) -> str:
    lat, lon = _coords(args)
    days = _int(args, "days", 1, MAX_DAYS, default=7)
    hours = _hours(args)
    try:
        # One more day than asked: the last night runs to the next morning.
        point = _points(_fetch_json(_forecast_url([lat], [lon], hourly=True, days=days + 1)), 1)[0]
    except _Transient as e:
        header, rows, fallback_hours = _fallback_or_error(lat, lon, e)
        # Before dawn the first row can be the rest of last night, dated
        # yesterday; it does not use up one of the days asked for.
        leftover = bool(rows) and all(" night" in line.split(":")[0] for iso, line in rows if iso == rows[0][0])
        dates = sorted({iso for iso, _ in rows})[:days + leftover]
        lines = [f"Open-Meteo unavailable ({e}); this is the fallback, with less detail.", header,
                 _alerts(lat, lon)]
        lines += [line for iso, line in rows if iso in dates]
        if hours is not None:
            lines += _fallback_hours(fallback_hours, hours)
        return "\n".join(lines)
    daily = point["daily"]
    tz = _zone(point)
    today, since = _since(tz)
    lines = [
        f"Forecast for {lat:g}, {lon:g} ({point.get('timezone', 'UTC')}), fetched {_fetched(point)}.",
        LEGEND,
        _alerts(lat, lon),
    ]
    dated = (daily.get("time") or [])[:days]
    lines += _periods(point, [date.fromisoformat(d) for d in dated[:PERIOD_DAYS]], since)
    lines += [_daily_line(daily, i) for i in range(PERIOD_DAYS, len(dated))]
    if hours is not None:
        hourly = point.get("hourly") or {}
        wanted = (today if hours == "today" else today + timedelta(days=1)).isoformat()
        lines.append(f"Hour by hour, {_weekday(wanted)}:")
        lines += [_hourly_line(hourly, i, tz) for i, t in enumerate(hourly.get("time") or [])
                  if t.startswith(wanted) and t >= since]
    return "\n".join(lines)


class _Stop(NamedTuple):
    lat: float
    lon: float
    when: date
    hour: int | None
    label: str | None
    brief: bool = False  # a town passed on the way: its arrival hour and alerts only
    # When the trip gets there, for a routed stop: the date and hour are
    # re-read in Open-Meteo's own zone for the point, which can differ from
    # the nearest town's (a zone line between them, or no town at all).
    at: datetime | None = None

    @property
    def name(self) -> str:
        return f"{self.label} ({self.lat:g}, {self.lon:g})" if self.label else f"{self.lat:g}, {self.lon:g}"


def _parse_stop(n: int, stop: Any) -> _Stop:
    if not isinstance(stop, dict):
        raise ToolError(f"stop {n} must be an object")
    try:
        _known(stop, TOOLS["route_forecast"][1]["inputSchema"]["properties"]["stops"]["items"])
        for key in ROUTED_ONLY:
            if key in stop:
                raise ToolError(f"{key} needs depart (the tool then routes and times the trip)")
        if "date" not in stop:
            raise ToolError("date is required, or pass depart to have the trip routed and timed")
        lat, lon = _coords(stop)
        return _Stop(lat, lon, _date(stop, "date", required=True), _int(stop, "hour", 0, 23),
                     _text(stop.get("label"), 60))
    except ToolError as e:
        raise ToolError(f"stop {n}: {e}") from None


def route_forecast(args: dict) -> str:
    stops = args.get("stops")
    if not isinstance(stops, list) or not stops:
        raise ToolError("stops must be a non-empty list")
    if len(stops) > MAX_STOPS:
        raise ToolError(f"at most {MAX_STOPS} stops per call")
    if "depart" in args:
        return _routed(args, stops)
    for key in ("start_hour", "max_drive_hours"):
        if key in args:
            raise ToolError(f"{key} needs depart (the tool then routes and times the trip)")
    return _stops_weather([_parse_stop(n, stop) for n, stop in enumerate(stops, 1)])


ROUTED_ONLY = ("overnight", "start_hour")


def _stops_weather(parsed: list[_Stop]) -> str:
    """Each stop's weather on its date, and at its hour when it has one."""
    # Only as many days as the furthest stop needs (+2: zones ahead of UTC,
    # and its night running into the next morning); a stop past 16 days is
    # refused by the range check either way.
    span = max((p.when - _now().date()).days for p in parsed) + 2
    try:
        points = _points(
            _fetch_json(_forecast_url([p.lat for p in parsed], [p.lon for p in parsed], hourly=True, days=span)),
            len(parsed),
        )
    except _Transient as e:
        # Brief points would spend the fallback's time budget on towns along
        # the way before reaching the destination, which is sorted last.
        return _route_fallback([p for p in parsed if not p.brief], e)
    alerts = _alerts_for([(p.lat, p.lon) for p in parsed])
    lines = [
        f"Route forecast, fetched {_fetched(points[0])}. Each stop is in its own local time.",
        LEGEND,
    ]
    for n, (stop, point) in enumerate(zip(parsed, points), 1):
        daily = point["daily"]
        if stop.at is not None:
            stop = stop._replace(**dict(zip(("when", "hour"), _on_the_hour(stop.at.astimezone(_zone(point))))))
        problem = _out_of_range(daily, stop.when)
        if problem:
            lines.append(f"{n}. {stop.name}: {problem}.")
            lines.append(f"   {alerts[(stop.lat, stop.lon)]}")
            continue
        tz = _zone(point)
        today, since = _since(tz)
        if stop.brief:
            # A town on the way: the hour you pass and its alerts, no more.
            hourly = point.get("hourly") or {}
            stamp, times = _hour_iso(stop.when, stop.hour or 0), hourly.get("time") or []
            if stamp in times:
                lines.append(f"{n}. {stop.name}, passing at {_hourly_line(hourly, times.index(stamp), tz)}")
            else:
                lines.append(f"{n}. {stop.name}, passing at {stamp[11:]}: not in the hourly data.")
            lines.append(f"   {alerts[(stop.lat, stop.lon)]}")
            continue
        if (stop.when - today).days < PERIOD_DAYS:
            # The stop's day and the night after it: where you sleep, if you do.
            lines.append(f"{n}. {stop.name}:")
            lines += [f"   {line}" for line in _periods(point, [stop.when], since, last_night=False)]
        else:
            lines.append(f"{n}. {stop.name}, {_daily_line(daily, daily['time'].index(stop.when.isoformat()))}")
        if stop.hour is not None:
            arrival = datetime(stop.when.year, stop.when.month, stop.when.day, stop.hour, tzinfo=tz)
            if arrival - _now() > timedelta(hours=ARRIVAL_HOUR_MAX_HOURS):
                lines.append(f"   Arrival-hour detail only within {ARRIVAL_HOUR_MAX_HOURS} hours of now; "
                             "the periods above stand.")
            else:
                hourly = point.get("hourly") or {}
                stamp = _hour_iso(stop.when, stop.hour)
                times = hourly.get("time") or []
                if stamp in times:
                    lines.append(f"   At {_hourly_line(hourly, times.index(stamp), tz)}")
                else:
                    lines.append(f"   At {stamp[11:]}: not in the hourly data.")
        lines.append(f"   {alerts[(stop.lat, stop.lon)]}")
    return "\n".join(lines)


def _route_fallback(parsed: list[_Stop], primary: ToolError) -> str:
    """One fallback lookup per distinct point; a stop that fails says so on
    its own line rather than costing the whole route."""
    lines = [f"Open-Meteo unavailable ({primary}); this is the fallback, with less detail."]
    cache: dict[tuple[float, float], Fallback | str] = {}
    # One work queue serializes every claude -p call, so a slow tool holds up
    # all of Marlow. Past the budget, the remaining stops are skipped.
    deadline = _clock() + FALLBACK_BUDGET_SECONDS
    for n, stop in enumerate(parsed, 1):
        key = (stop.lat, stop.lon)
        if key not in cache and _clock() > deadline:
            cache[key] = "unavailable (fallback time budget spent)"
        if key not in cache:
            try:
                cache[key] = _fallback(stop.lat, stop.lon)
            except (ToolError, KeyError, TypeError, ValueError, AttributeError) as e:
                cache[key] = f"unavailable ({e if isinstance(e, ToolError) else type(e).__name__})"
        found = cache[key]
        if isinstance(found, str):
            lines.append(f"{n}. {stop.name}: {found}.")
            continue
        lines.append(f"{n}. {stop.name}: {_rows_for(found.rows, stop.when).rstrip('.')}. Source: {found.header}")
        if stop.hour is not None:
            stamp = _hour_iso(stop.when, stop.hour)
            if not found.hours:
                lines.append(f"   At {stamp[11:]}: no hourly data from this fallback.")
            elif stamp in found.hours:
                lines.append(f"   At {found.hours[stamp]}")
            else:
                last = max(found.hours)
                lines.append(f"   At {stamp[11:]}: outside the fallback's hourly data "
                             f"({_weekday(min(found.hours)[:10])} {min(found.hours)[11:16]} to "
                             f"{_weekday(last[:10])} {last[11:16]}).")
    alerts = _alerts_for([(p.lat, p.lon) for p in parsed])
    lines += [f"Stop {n} {alerts[(p.lat, p.lon)]}" for n, p in enumerate(parsed, 1)]
    lines.append("If a stop is unavailable, say so; do not search for a forecast.")
    return "\n".join(lines)


_COORD_SCHEMA = {
    "latitude": {"type": "number", "minimum": -90, "maximum": 90},
    "longitude": {"type": "number", "minimum": -180, "maximum": 180},
}

# Names a conversation may still hold from an earlier version of these tools.
# Ignoring them answered a question that was never asked: a stop with
# from_hour 18 / to_hour 8 got the plain stop's periods and no word that the
# window had gone (Marlow, 2026-10-13).
RETIRED = {
    "from_hour": "each stop already gets its day (06:00-18:00) and the night after it (18:00-06:00)",
    "to_hour": "each stop already gets its day (06:00-18:00) and the night after it (18:00-06:00)",
    "hourly_date": "use hours: 'today' or 'tomorrow'",
}


def _known(args: dict, schema: dict) -> None:
    for key in args:
        if key not in schema["properties"]:
            hint = RETIRED.get(key, "see the tool's schema")
            raise ToolError(f"unknown argument {key!r}: {hint}")


TOOLS: dict[str, tuple[Callable[[dict], str], dict]] = {
    "geocode": (geocode, {
        "description": (
            "Look up a place name and get its latitude/longitude. Only for places "
            "you do not already have coordinates for: the [Context] line carries "
            "the user's own position when the app sends it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Town or city, e.g. 'Wawa'"},
                "country_code": {"type": "string", "description": "ISO 2-letter filter, e.g. 'CA'"},
            },
            "required": ["name"],
        },
    }),
    "forecast": (forecast, {
        "description": (
            "Current forecast for one point, in the place's local time: day and "
            "night periods for the first week, then one line per day (up to 16 "
            "days), plus active weather alerts (Canada and US). Use this for any "
            "weather question, never a web search."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **_COORD_SCHEMA,
                "days": {"type": "integer", "minimum": 1, "maximum": MAX_DAYS, "default": 7},
                "hours": {"type": "string", "enum": list(HOURS_CHOICES),
                          "description": "adds hour-by-hour detail for that day, e.g. to pick a "
                                         "morning or an afternoon"},
            },
            "required": ["latitude", "longitude"],
        },
    }),
    "route_forecast": (route_forecast, {
        "description": (
            "Plan a drive and its weather in one call. Give the stops in order "
            "(start, any you want to pass, destination) and depart: the tool routes "
            "it by road, times every stop in its own local time, splits the drive "
            "into days at max_drive_hours (suggesting an overnight near the cap, "
            "with the bigger towns before it as options), names the towns on the way, flags borders and "
            "ferries, and gives the weather and alerts (Canada and US) at the start, "
            "each overnight and the destination at the hour you get there. To "
            "choose an overnight, call again with it as a stop with overnight: true. "
            "Without depart, give each stop a date (and hour) and it forecasts those."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "depart": {"type": "string",
                           "description": "YYYY-MM-DDTHH:MM, local at the first stop: routes and times the trip. "
                                          "The time they said they're leaving; if they didn't say, pick one and "
                                          "tell them it's your assumption"},
                "start_hour": {"type": "integer", "minimum": 0, "maximum": 23, "default": DEFAULT_START_HOUR,
                               "description": "when each day after an overnight starts, local: the user's "
                                              "usual start, from what they've said or memory; otherwise leave "
                                              "it out and the default is used and stated"},
                "max_drive_hours": {"type": "number", "minimum": 1, "maximum": 16,
                                    "default": DEFAULT_MAX_DRIVE_HOURS,
                                    "description": "hours of driving a day: the user's usual, from what "
                                                   "they've said or memory; otherwise leave it out and the "
                                                   "default is used and stated"},
                "stops": {
                    "type": "array",
                    "maxItems": MAX_STOPS,
                    "items": {
                        "type": "object",
                        "properties": {
                            **_COORD_SCHEMA,
                            "date": {"type": "string", "description": "YYYY-MM-DD, local; only without depart"},
                            "hour": {"type": "integer", "minimum": 0, "maximum": 23,
                                     "description": "arrival hour, local: adds that hour's detail "
                                                    f"(within {ARRIVAL_HOUR_MAX_HOURS} hours of now)"},
                            "label": {"type": "string", "description": "e.g. 'Wawa'"},
                            "overnight": {"type": "boolean",
                                          "description": "with depart: the day's drive ends here"},
                            "start_hour": {"type": "integer", "minimum": 0, "maximum": 23,
                                           "description": "on an overnight: when the next day starts"},
                        },
                        "required": ["latitude", "longitude"],
                    },
                },
            },
            "required": ["stops"],
        },
    }),
}


# --- transport -------------------------------------------------------------------

def handle(message: Any) -> dict | None:
    """One JSON-RPC message in, one response out (None for a notification)."""
    if not isinstance(message, dict) or "id" not in message:
        return None
    msg_id, method = message["id"], message.get("method")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}

    def ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

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
        return ok({"tools": [{"name": name, **spec} for name, (_, spec) in TOOLS.items()]})
    if method == "tools/call":
        entry = TOOLS.get(params.get("name"))
        if entry is None:
            return {"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32602, "message": f"unknown tool {params.get('name')!r}"}}
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        try:
            _known(arguments, entry[1]["inputSchema"])
            return ok({"content": [{"type": "text", "text": entry[0](arguments)}], "isError": False})
        except ToolError as e:
            return ok({"content": [{"type": "text", "text": f"Weather error: {e}"}], "isError": True})
        except Exception as e:  # a formatting bug costs one answer, not the server
            log.error("INTERNAL %s: %s: %s", params.get("name"), type(e).__name__, e, exc_info=True)
            return ok({"content": [{"type": "text", "text": f"Weather error: internal ({type(e).__name__})"}],
                       "isError": True})
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"unknown method {method!r}"}}


def _log_to(argv: list[str]) -> None:
    """`--log PATH` (invoke.py passes it): problems to that file. Without it,
    as in the tests, the logger is left to whoever configures logging."""
    if "--log" not in argv or argv.index("--log") + 1 >= len(argv):
        return
    try:
        handler = logging.FileHandler(argv[argv.index("--log") + 1], mode="a")
    except OSError as e:  # never cost the forecast over its log
        sys.stderr.write(f"weather: cannot open log ({e})\n")
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


def main() -> None:
    _log_to(sys.argv[1:])
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            response = handle(message)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
