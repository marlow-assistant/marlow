"""server/weather_mcp.py (docs/tasks/weather-mcp.md).

No network: every fetch is served from tests/fixtures/weather/, recorded from
the live services on 2026-10-13. The transport tests run the real file in a
subprocess with `-I`, as invoke.py does.
"""
from __future__ import annotations

import copy
import io
import json
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import pytest

from server import boundaries, weather_mcp as w

FIXTURES = Path(__file__).parent / "fixtures" / "weather"
SCRIPT = Path(w.__file__)
NOW = datetime(2026, 10, 13, 12, 30, tzinfo=timezone.utc)


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


class _Calls(list):
    """Every URL fetched, in order; `routes` answers them by substring."""

    def __init__(self) -> None:
        super().__init__()
        self.routes: list[tuple[str, object]] = []


@pytest.fixture
def calls(monkeypatch):
    seen = _Calls()
    routes = seen.routes

    # Consulted after a test's own routes: the alerts line asks Environment
    # Canada and NWS on every answer, and most tests are not about alerts.
    defaults = [("api.weather.gc.ca", {"features": []}), ("api.weather.gov/alerts", {"features": []}),
                ("api.weather.gov/points", w._HTTPStatus("api.weather.gov returned HTTP 404", 404))]

    def fake(url: str, **_options):
        seen.append(url)
        for needle, answer in [*routes, *defaults]:
            if needle in url:
                if isinstance(answer, Exception):
                    raise answer
                return answer() if callable(answer) else copy.deepcopy(answer)
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(w, "_fetch_json", fake)
    monkeypatch.setattr(w, "_now", lambda: NOW)
    monkeypatch.setattr(w, "_sleep", lambda _s: None)
    w._ECCC_PAGES.clear()
    w._ECCC_NEAREST.clear()
    return seen


def _multi():
    """The two-point recording has no hourly block (it predates periods);
    lend each point the single-point recording's, which is Port Elgin's."""
    payload = _fixture("forecast_multi.json")
    hourly = _fixture("forecast_single.json")["hourly"]
    for point in payload:
        point["hourly"] = copy.deepcopy(hourly)
    return payload


OUTAGE = w._Transient("api.open-meteo.com returned HTTP 503: The service is overloaded", 503)


# --- transport ------------------------------------------------------------------

def _converse(*messages: str) -> list[dict]:
    done = subprocess.run(
        [sys.executable, "-I", str(SCRIPT)],
        input="\n".join(messages) + "\n", capture_output=True, text=True, timeout=20,
    )
    assert done.returncode == 0, done.stderr
    return [json.loads(line) for line in done.stdout.splitlines()]


def test_stdio_handshake_list_and_errors_over_a_real_pipe():
    replies = _converse(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),  # no reply
        "this is not json",
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "resources/list"}),
        # Invalid arguments fail before any fetch, so this needs no network.
        json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {"name": "forecast", "arguments": {"latitude": True, "longitude": 0}}}),
    )
    assert [r.get("id") for r in replies] == [1, None, 2, 3, 4]
    assert replies[0]["result"]["protocolVersion"] == "2025-06-18"
    assert replies[1]["error"]["code"] == -32700
    assert [t["name"] for t in replies[2]["result"]["tools"]] == ["geocode", "forecast", "route_forecast"]
    assert replies[3]["error"]["code"] == -32601
    assert replies[4]["result"]["isError"] is True
    assert "latitude must be a number" in replies[4]["result"]["content"][0]["text"]


def test_unknown_protocol_version_gets_ours():
    reply = w.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999"}})
    assert reply["result"]["protocolVersion"] == w.PROTOCOL_VERSIONS[0]


def test_a_formatting_bug_costs_one_answer_not_the_server(monkeypatch):
    monkeypatch.setitem(w.TOOLS, "forecast", (lambda _a: 1 / 0, w.TOOLS["forecast"][1]))
    reply = w.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                      "params": {"name": "forecast", "arguments": {}}})
    assert reply["result"]["isError"] and "ZeroDivisionError" in reply["result"]["content"][0]["text"]


def _call(name, arguments):
    reply = w.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": name, "arguments": arguments}})
    return reply["result"]["isError"], reply["result"]["content"][0]["text"]


def test_an_argument_from_an_older_schema_is_refused_with_what_replaced_it(calls):
    # Marlow sent a stop with from_hour/to_hour after they were retired, its
    # conversation remembering the old tool; ignored, it got the plain stop
    # and no word that its window had gone (2026-10-13).
    stop = {"latitude": 48.72, "longitude": -86.38, "date": "2026-10-14"}
    error, text = _call("route_forecast", {"stops": [stop, {**stop, "from_hour": 18, "to_hour": 8}]})
    assert error and text.startswith("Weather error: stop 2: unknown argument 'from_hour': ")
    assert "the night after it (18:00-06:00)" in text
    error, text = _call("forecast", {"latitude": 46.5, "longitude": -84.3, "hourly_date": "2026-10-13"})
    assert error and "unknown argument 'hourly_date': use hours: 'today' or 'tomorrow'" in text
    error, text = _call("geocode", {"name": "Wawa", "region": "ON"})
    assert error and "unknown argument 'region': see the tool's schema" in text
    assert calls == []


# --- forecast (Open-Meteo) --------------------------------------------------------

def test_forecast_rows_are_dated_local_and_name_their_model(calls):
    calls.routes.append(("api.open-meteo.com", _fixture("forecast_single.json")))
    out = w.forecast({"latitude": 44.49, "longitude": -81.38, "days": 16})
    lines = out.splitlines()
    assert lines[0] == "Forecast for 44.49, -81.38 (America/Toronto), fetched Tue 2026-10-13 08:30 EDT."
    rows = [l for l in lines if l[:3] in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")]
    # A week of day/night pairs (it is 08:30, so no rest-of-last-night), then
    # one line per day; the fixture's 16th day is the last there is.
    assert [r.split(":")[0] for r in rows[:3]] == [
        "Tue 2026-10-13 day 08", "Tue 2026-10-13 night 18", "Wed 2026-10-14 day 06"]
    assert len(rows) == 14 + 9 and rows[13].startswith("Mon 2026-10-19 night ")
    assert rows[14].startswith("Tue 2026-10-20: ") and ", worst: " in rows[14]
    # GEM runs out after 9 days; the rest is best_match, and each row says so.
    assert all(r.endswith("[GEM]") for r in rows[:16])
    assert all(r.endswith("[best match]") for r in rows[16:])


def test_coordinates_are_rounded_before_they_leave_the_box(calls):
    calls.routes.append(("api.open-meteo.com", _fixture("forecast_single.json")))
    w.forecast({"latitude": 44.494949, "longitude": -81.383838})
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(calls[0]).query)
    assert query["latitude"] == ["44.49"] and query["longitude"] == ["-81.38"]
    assert query["models"] == ["gem_seamless,best_match"]


def test_a_missing_value_is_a_dash_never_zero(calls):
    payload = _fixture("forecast_single.json")
    payload["daily"]["precipitation_sum_gem_seamless"][7] = None
    payload["hourly"]["precipitation_gem_seamless"] = [None] * len(payload["hourly"]["time"])
    calls.routes.append(("api.open-meteo.com", payload))
    out = w.forecast({"latitude": 44.49, "longitude": -81.38, "days": 8})
    assert "Tue 2026-10-20: 6–14°C, worst: mainly clear, – total, 6% chance," in out
    assert ", – total, up to 72% chance," in out.split("Tue 2026-10-13 day ")[1].split("\n")[0]


def test_a_day_period_summarises_its_hours_and_starts_now_today(calls):
    fixture = _fixture("forecast_single.json")
    calls.routes.append(("api.open-meteo.com", fixture))
    rows = w.forecast({"latitude": 44.49, "longitude": -81.38, "days": 2}).splitlines()[3:]
    today = _expected(fixture, "2026-10-13T08:00", "2026-10-13T17:00", "temperature_2m")
    assert rows[0].startswith(f"Tue 2026-10-13 day 08:00–18:00 EDT: {min(today):.0f}–{max(today):.0f}°C, ")
    assert ", sun 07:38–18:44 [GEM]" in rows[0]
    tomorrow = _expected(fixture, "2026-10-14T06:00", "2026-10-14T17:00", "temperature_2m")
    gusts = _expected(fixture, "2026-10-14T06:00", "2026-10-14T17:00", "wind_gusts_10m")
    assert rows[2].startswith(f"Wed 2026-10-14 day 06:00–18:00 EDT: {min(tomorrow):.0f}–{max(tomorrow):.0f}°C, ")
    assert f" gusting {max(gusts):.0f} km/h" in rows[2]
    assert "hourly" in urllib.parse.parse_qs(urllib.parse.urlsplit(calls[0]).query)


def test_a_night_runs_to_six_the_next_morning(calls):
    fixture = _fixture("forecast_single.json")
    calls.routes.append(("api.open-meteo.com", fixture))
    rows = w.forecast({"latitude": 44.49, "longitude": -81.38, "days": 2}).splitlines()[3:]
    night = _expected(fixture, "2026-10-14T18:00", "2026-10-15T05:00", "temperature_2m")
    rain = _expected(fixture, "2026-10-14T18:00", "2026-10-15T05:00", "precipitation")
    assert rows[3].startswith(f"Wed 2026-10-14 night 18:00–06:00 EDT: {min(night):.0f}–{max(night):.0f}°C, ")
    assert f", {sum(rain):.1f} mm total, " in rows[3] and ", sun " not in rows[3]
    assert len(rows) == 4, "the request asked one day more, for the last night's morning"


def test_before_dawn_the_first_period_is_the_rest_of_last_night(calls, monkeypatch):
    monkeypatch.setattr(w, "_now", lambda: datetime(2026, 10, 13, 6, 10, tzinfo=timezone.utc))  # 02:10 EDT
    calls.routes.append(("api.open-meteo.com", _fixture("forecast_single.json")))
    rows = w.forecast({"latitude": 44.49, "longitude": -81.38, "days": 1}).splitlines()[3:]
    assert [r.split(" EDT")[0] for r in rows] == [
        "Mon 2026-10-12 night 02:00–06:00", "Tue 2026-10-13 day 06:00–18:00", "Tue 2026-10-13 night 18:00–06:00"]


def test_after_dark_today_is_just_tonight(calls, monkeypatch):
    monkeypatch.setattr(w, "_now", lambda: datetime(2026, 10, 14, 1, 30, tzinfo=timezone.utc))  # 21:30 EDT
    calls.routes.append(("api.open-meteo.com", _fixture("forecast_single.json")))
    rows = w.forecast({"latitude": 44.49, "longitude": -81.38, "days": 1}).splitlines()[3:]
    assert [r.split(" EDT")[0] for r in rows] == ["Tue 2026-10-13 night 21:00–06:00"]


def test_a_period_the_data_stops_partway_through_says_so():
    hourly = {"time": [f"2026-10-14T{h:02d}:00" for h in range(24)],
              "temperature_2m_gem_seamless": [5.0] * 24}
    tz = w.ZoneInfo("America/Toronto")
    line = w._period_line(hourly, tz, w.date(2026, 10, 14), True, "2026-10-14T00:00")
    assert line.startswith("Wed 2026-10-14 night 18:00–06:00 (data ends 23:00) EDT: 5–5°C, ")
    assert w._period_line(hourly, tz, w.date(2026, 10, 20), True, "2026-10-14T00:00") is None


def test_a_period_names_its_three_worst_conditions_in_order_and_when():
    codes = [3, 2, 95, 1, 61, 0, 61, 45]
    hourly = {"time": [f"2026-10-14T{h:02d}:00" for h in range(6, 6 + len(codes))],
              "temperature_2m_gem_seamless": [5.0] * len(codes), "weather_code_gem_seamless": codes}
    line = w._period_line(hourly, w.ZoneInfo("America/Toronto"), w.date(2026, 10, 14), False, "2026-10-14T00:00")
    # A code at T is the hour ending at T, as the rain is: 06:00's is night.
    assert "°C, thunderstorm 07:00–08:00, light rain 09:00–10:00 and 11:00–12:00, fog 12:00–13:00, " in line
    hourly["weather_code_gem_seamless"] = [61, 61, 3, 61, 3, 61, 61, 3]
    line = w._period_line(hourly, w.ZoneInfo("America/Toronto"), w.date(2026, 10, 14), False, "2026-10-14T00:00")
    assert "°C, light rain on and off 06:00–12:00, overcast on and off 07:00–13:00, " in line


def test_hour_by_hour_is_today_from_now_or_all_of_tomorrow(calls):
    calls.routes.append(("api.open-meteo.com", _fixture("forecast_single.json")))
    out = w.forecast({"latitude": 44.49, "longitude": -81.38, "days": 1, "hours": "today"})
    hours = out.split("Hour by hour, Tue 2026-10-13:\n")[1].splitlines()
    assert [h[:9] for h in hours] == [f"{h:02d}:00 EDT" for h in range(8, 24)]
    out = w.forecast({"latitude": 44.49, "longitude": -81.38, "days": 1, "hours": "tomorrow"})
    hours = out.split("Hour by hour, Wed 2026-10-14:\n")[1].splitlines()
    assert [h[:9] for h in hours] == [f"{h:02d}:00 EDT" for h in range(24)]


def test_the_zone_abbreviation_is_the_one_in_force_at_that_hour():
    tz = w.ZoneInfo("America/Toronto")
    assert w._abbr(tz, "2026-10-31T12:00") == "EDT"
    assert w._abbr(tz, "2026-11-02T12:00") == "EST"


@pytest.mark.parametrize("args, message", [
    ({"latitude": 91, "longitude": 0}, "latitude must be between"),
    ({"latitude": float("nan"), "longitude": 0}, "latitude must be between"),
    ({"latitude": 0, "longitude": 0, "days": 17}, "days must be a whole number"),
    ({"latitude": 0, "longitude": 0, "hours": "2026-10-14"}, "hours must be 'today' or 'tomorrow'"),
    ({"latitude": 0, "longitude": 0, "hours": "friday"}, "hours must be 'today' or 'tomorrow'"),
])
def test_bad_forecast_arguments_never_fetch(calls, args, message):
    with pytest.raises(w.ToolError, match=message):
        w.forecast(args)
    assert calls == []


# --- retries and fallback triggers ----------------------------------------------------

def test_one_retry_on_a_transient_failure(monkeypatch):
    attempts = []

    def flaky(url):
        attempts.append(url)
        if len(attempts) == 1:
            raise w._Transient("overloaded", 503)
        return {"ok": True}

    slept = []
    monkeypatch.setattr(w, "_fetch_once", flaky)
    monkeypatch.setattr(w, "_sleep", slept.append)
    assert w._fetch_json("https://api.open-meteo.com/x") == {"ok": True}
    assert len(attempts) == 2 and slept == [w.RETRY_DELAY_SECONDS]


def _failing(monkeypatch, *errors):
    """_fetch_once raising each error in turn, then answering."""
    attempts, slept = [], []

    def fetch(url, timeout=None):
        attempts.append(url)
        if len(attempts) <= len(errors):
            raise errors[len(attempts) - 1]
        return {"ok": True}

    monkeypatch.setattr(w, "_fetch_once", fetch)
    monkeypatch.setattr(w, "_sleep", slept.append)
    return attempts, slept


def test_a_timeout_goes_straight_to_the_fallback(monkeypatch, caplog):
    attempts, slept = _failing(monkeypatch, w._Timeout("api.open-meteo.com unreachable (TimeoutError)", 0))
    with pytest.raises(w._Transient):
        w._fetch_json("https://api.open-meteo.com/x")
    assert len(attempts) == 1 and slept == []
    assert _logged(caplog) == ["FETCH api.open-meteo.com/x failed (1 attempt): "
                               "api.open-meteo.com unreachable (TimeoutError)"]


def test_a_retry_honours_a_short_retry_after_and_skips_a_long_one(monkeypatch, caplog):
    attempts, slept = _failing(monkeypatch, w._Transient("busy", 429, retry_after=2.0))
    assert w._fetch_json("https://api.open-meteo.com/x") == {"ok": True}
    assert slept == [2.0]
    assert _logged(caplog) == ["RETRY ok api.open-meteo.com/x after: busy"]
    attempts, slept = _failing(monkeypatch, w._Transient("busy", 503, retry_after=60.0))
    with pytest.raises(w._Transient):
        w._fetch_json("https://api.open-meteo.com/x")
    assert len(attempts) == 1 and slept == []


@pytest.mark.parametrize("header, wait", [("2", 2.0), (None, None), ("Wed, 21 Oct 2026 07:28:00 GMT", None)])
def test_retry_after_is_read_from_a_503(monkeypatch, header, wait):
    def overloaded(*_a, **_k):
        raise w.urllib.error.HTTPError("https://api.open-meteo.com/x", 503, "busy",
                                       {"Retry-After": header} if header else {}, io.BytesIO(b"{}"))
    monkeypatch.setattr(w.urllib.request, "urlopen", overloaded)
    with pytest.raises(w._Transient) as raised:
        w._fetch_once("https://api.open-meteo.com/x")
    assert raised.value.retry_after == wait


def test_both_kinds_of_timeout_are_timeouts(monkeypatch):
    for error in (TimeoutError("read"), w.urllib.error.URLError(TimeoutError("connect"))):
        def boom(*_a, error=error, **_k):
            raise error
        monkeypatch.setattr(w.urllib.request, "urlopen", boom)
        with pytest.raises(w._Timeout, match=r"unreachable \(TimeoutError\)"):
            w._fetch_once("https://api.open-meteo.com/x")
    monkeypatch.setattr(w.urllib.request, "urlopen",
                        lambda *_a, **_k: (_ for _ in ()).throw(w.urllib.error.URLError(ConnectionRefusedError())))
    with pytest.raises(w._Transient) as raised:
        w._fetch_once("https://api.open-meteo.com/x")
    assert not isinstance(raised.value, w._Timeout), "a refused connection is quick: worth the retry"


def test_a_bad_request_is_not_retried_and_not_a_fallback(calls):
    calls.routes.append(("api.open-meteo.com", w._HTTPStatus("api.open-meteo.com returned HTTP 400", 400)))
    with pytest.raises(w.ToolError, match="HTTP 400"):
        w.forecast({"latitude": 44.49, "longitude": -81.38})
    assert len(calls) == 1, "a 400 is our bug: no national service should be asked"


# --- fallbacks -----------------------------------------------------------------

def _eccc_routes(calls, item=None):
    calls.routes.append(("api.open-meteo.com", OUTAGE))
    calls.routes.append(("items/on-19", item or _fixture("eccc_item.json")))
    calls.routes.append(("properties=identifier", _fixture("eccc_list.json")))


def test_canada_falls_back_to_environment_canada_with_dates(calls):
    _eccc_routes(calls)
    out = w.forecast({"latitude": 44.49, "longitude": -81.38, "days": 2, "hours": "tomorrow"})
    lines = out.splitlines()
    assert lines[0].startswith("Open-Meteo unavailable (api.open-meteo.com returned HTTP 503")
    assert lines[1] == ("Environment Canada forecast for Port Elgin (Saugeen Shores - Kincardine - Southern "
                        "Bruce County; nearest city page, 6 km away), issued Tue 2026-10-13 05:00 EDT.")
    assert lines[2] == "Alerts: none in effect (Environment Canada, at this point)."
    assert [l.split(":")[0] for l in lines[3:7]] == [
        "Tue 2026-10-13 day", "Tue 2026-10-13 night", "Wed 2026-10-14 day", "Wed 2026-10-14 night"]
    # The page's 24 hours run 09:00 Tuesday to 08:00 Wednesday, EDT.
    assert lines[7] == "Hour by hour, Wed 2026-10-14 (the fallback's hourly data ends 08:00):"
    assert [l[:9] for l in lines[8:]] == [f"{h:02d}:00 EDT" for h in range(9)]
    assert lines[-1] == "08:00 EDT: 13°C, Chance of showers or drizzle, 40% chance, wind NW 30 km/h [Environment Canada]"
    assert sum("items/on-19" in url for url in calls) == 1, "the fallback and the alerts share the page"


def test_an_environment_canada_id_that_is_not_an_id_is_never_fetched(calls):
    listing = _fixture("eccc_list.json")
    for feature in listing["features"]:
        feature["id"] = "../../evil"
    calls.routes.append(("api.open-meteo.com", OUTAGE))
    calls.routes.append(("api.weather.gc.ca", listing))
    calls.routes.append(("api.weather.gov/points", w._HTTPStatus("404", 404)))
    with pytest.raises(w.ToolError, match="no fallback covers this point"):
        w.forecast({"latitude": 44.49, "longitude": -81.38})
    assert not any("evil" in url for url in calls)


def test_the_uk_falls_back_to_met_norway_in_uk_time(calls):
    calls.routes += [("api.open-meteo.com", OUTAGE), ("api.weather.gc.ca", {"features": []}),
                     ("api.met.no", _fixture("metno_london.json"))]
    lines = w.forecast({"latitude": 51.51, "longitude": -0.13, "days": 2}).splitlines()
    assert lines[1] == "MET Norway forecast (UK fallback), updated Tue 2026-10-13 12:23 BST."
    assert lines[2] == "Alerts: not checked (no keyless UK warnings feed)."
    # Day and night, like every other answer; the night runs past midnight.
    assert [l.split(":")[0] for l in lines[3:]] == [
        "Tue 2026-10-13 day (from 13", "Tue 2026-10-13 night", "Wed 2026-10-14 day", "Wed 2026-10-14 night"]
    assert "clear sky" in lines[3]
    assert not any("api.weather.gov" in url for url in calls)


def test_met_norway_drops_a_lone_sample_period_and_never_invents_dry(calls):
    """Review 2026-10-13: the series' last lone step rendered as a whole
    day, "9–9°C, –, 0.0 mm"."""
    payload = _fixture("metno_london.json")
    series = payload["properties"]["timeseries"]
    lone = copy.deepcopy(series[-1])
    lone["time"] = "2026-10-23T06:00:00Z"  # 07:00 BST: Friday's day, alone
    series.append(lone)
    calls.routes += [("api.open-meteo.com", OUTAGE), ("api.weather.gc.ca", {"features": []}),
                     ("api.met.no", payload)]
    lines = w.forecast({"latitude": 51.51, "longitude": -0.13, "days": 16}).splitlines()
    assert lines[-1].startswith("Thu 2026-10-22 night: "), "one sample is not a period"
    london = w.ZoneInfo("Europe/London")
    for step in series:
        moment = w._utc(step["time"]).astimezone(london)
        if moment.date().isoformat() == "2026-10-14" and 6 <= moment.hour < 18:
            step["data"].pop("next_1_hours", None)
            step["data"].pop("next_6_hours", None)
    calls.routes[-1] = ("api.met.no", payload)
    wednesday = next(l for l in w.forecast({"latitude": 51.51, "longitude": -0.13, "days": 3}).splitlines()
                     if l.startswith("Wed 2026-10-14 day"))
    assert ", – , " not in wednesday and ", –, wind" in wednesday


@pytest.mark.parametrize("lat, lon, uk", [
    (51.51, -0.13, True),    # London
    (53.35, -6.26, True),    # Dublin: same clock all year
    (51.13, 1.31, True),     # Dover
    (50.73, 1.61, False),    # Boulogne
    (49.92, 1.08, False),    # Dieppe
    (48.85, 2.35, False),    # Paris
])
def test_the_uk_box(lat, lon, uk):
    assert w._in_uk(lat, lon) is uk


def test_the_us_falls_back_to_the_nws_in_celsius(calls):
    calls.routes += [("api.open-meteo.com", OUTAGE), ("api.weather.gc.ca", {"features": []}),
                     ("api.weather.gov/points/46.5000,-84.3500", _fixture("nws_points_soo.json")),
                     ("api.weather.gov/gridpoints/APX/65,131/forecast?units=si", _fixture("nws_forecast_soo.json"))]
    lines = w.forecast({"latitude": 46.5, "longitude": -84.35, "days": 1}).splitlines()
    assert lines[1] == ("US National Weather Service forecast near Sault Ste. Marie, MI, "
                        "updated Tue 2026-10-13 08:09 EDT.")
    assert lines[2] == "Alerts: none in effect (US National Weather Service)."
    assert lines[3] == ("Tue 2026-10-13 day: high 13°C, Rain Showers, 79% chance of precipitation, "
                        "wind NE 5 to 15 km/h")


def test_the_nws_forecast_link_is_only_followed_to_the_nws(calls):
    points = _fixture("nws_points_soo.json")
    points["properties"]["forecast"] = "https://evil.example/gridpoints/x"
    calls.routes += [("api.open-meteo.com", OUTAGE), ("api.weather.gc.ca", {"features": []}),
                     ("api.weather.gov/points", points)]
    with pytest.raises(w.ToolError, match="fallback failed too: the NWS returned an unexpected forecast link"):
        w.forecast({"latitude": 46.5, "longitude": -84.35})
    assert not any("evil" in url for url in calls)


def test_elsewhere_says_unavailable_and_not_to_search(calls):
    calls.routes += [("api.open-meteo.com", OUTAGE), ("api.weather.gc.ca", {"features": []}),
                     ("api.weather.gov/points", w._HTTPStatus("404", 404))]
    with pytest.raises(w.ToolError) as raised:
        w.forecast({"latitude": 48.85, "longitude": 2.35})
    assert "Canada, the UK and the US" in str(raised.value)
    assert "do not search" in str(raised.value)


def test_a_fallback_of_the_wrong_shape_is_an_error_line_not_a_crash(calls):
    calls.routes += [("api.open-meteo.com", OUTAGE), ("api.weather.gc.ca", {"features": []}),
                     ("api.met.no", {"unexpected": True})]
    with pytest.raises(w.ToolError, match="unexpected shape"):
        w.forecast({"latitude": 51.51, "longitude": -0.13})


# --- route_forecast -------------------------------------------------------------

def test_a_route_is_one_request_with_each_stop_on_its_date(calls):
    calls.routes.append(("api.open-meteo.com", _multi()))
    out = w.route_forecast({"stops": [
        {"latitude": 44.49, "longitude": -81.38, "date": "2026-10-15", "label": "Home"},
        {"latitude": 46.52, "longitude": -84.35, "date": "2026-11-02", "label": "Soo"},
    ]})
    assert sum("open-meteo" in url for url in calls) == 1
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(calls[0]).query)
    assert query["latitude"] == ["44.49,46.52"]
    lines = out.splitlines()
    # The stop's day and the night after it: the night at camp, by default.
    assert lines[2] == "1. Home (44.49, -81.38):"
    assert lines[3].startswith("   Thu 2026-10-15 day 06:00–18:00 EDT: ")
    assert lines[4].startswith("   Thu 2026-10-15 night 18:00–06:00 EDT: ")
    assert ("2. Soo (46.52, -84.35): 2026-11-02 is beyond the forecast (it ends 2026-10-28; for dates "
            "that far out, typical conditions for the month are the best guide).") in out


def test_a_stop_past_the_first_week_gets_one_line_for_its_day(calls):
    calls.routes.append(("api.open-meteo.com", _multi()[0]))
    out = w.route_forecast({"stops": [{"latitude": 44.49, "longitude": -81.38, "date": "2026-10-21"}]})
    assert "1. 44.49, -81.38, Wed 2026-10-21: 4–16°C, worst: clear, " in out


def test_an_arrival_hour_reaches_72_hours_from_now(calls):
    # NOW is Tue 08:30 EDT, so the cap is Fri 08:30 EDT: by the clock, not
    # by calendar day.
    calls.routes.append(("api.open-meteo.com", _multi()))
    out = w.route_forecast({"stops": [
        {"latitude": 44.49, "longitude": -81.38, "date": "2026-10-16", "hour": 8},
        {"latitude": 46.52, "longitude": -84.35, "date": "2026-10-16", "hour": 9},
    ]})
    assert "   At 08:00 EDT: " in out.split("2. ")[0]
    assert "   Arrival-hour detail only within 72 hours of now; the periods above stand." in out.split("2. ")[1]


def test_a_route_falls_back_per_stop(calls):
    _eccc_routes(calls)
    calls.routes.insert(1, ("api.weather.gov/points", w._HTTPStatus("404", 404)))
    out = w.route_forecast({"stops": [
        {"latitude": 44.49, "longitude": -81.38, "date": "2026-10-14", "label": "Port Elgin", "hour": 14},
        {"latitude": 48.85, "longitude": 2.35, "date": "2026-10-14", "label": "Paris"},
        {"latitude": 44.49, "longitude": -81.38, "date": "2026-10-30"},
    ]})
    assert "1. Port Elgin (44.49, -81.38): Wed 2026-10-14 day: " in out
    assert "2. Paris (48.85, 2.35): unavailable (no fallback covers this point" in out
    assert "3. 44.49, -81.38: 2026-10-30 is beyond this source's forecast (it ends 2026-10-19; for dates" in out
    assert sum("items/on-19" in url for url in calls) == 1, "a repeated point is looked up once"
    assert "   At 14:00: outside the fallback's hourly data (Tue 2026-10-13 09:00 to Wed 2026-10-14 08:00)." in out


def test_a_route_fallback_gives_the_arrival_hour_from_environment_canada(calls):
    _eccc_routes(calls)
    out = w.route_forecast({"stops": [{"latitude": 44.49, "longitude": -81.38, "date": "2026-10-13", "hour": 10}]})
    assert "\n   At 10:00 EDT: 22°C, Mainly cloudy, 20% chance, wind SW 20 gusting 40 km/h [Environment Canada]\n" in out
    calls.routes[:] = [("api.open-meteo.com", OUTAGE), ("api.weather.gc.ca", {"features": []}),
                       ("api.met.no", _fixture("metno_london.json"))]
    out = w.route_forecast({"stops": [{"latitude": 51.51, "longitude": -0.13, "date": "2026-10-13", "hour": 20}]})
    assert "   At 20:00: no hourly data from this fallback." in out


def _far_listing(km_north, lat=44.49, lon=-81.38):
    # One page due north of the point, at that distance.
    return {"features": [{"id": "on-19", "type": "Feature",
                          "geometry": {"type": "Point", "coordinates": [lon, lat + km_north / 111.2]},
                          "properties": {"identifier": "on-19"}}]}


def test_a_far_city_page_is_a_regional_guide_and_comes_after_the_nws(calls, caplog):
    """Valemount's own page is pinned 80 km away, on McBride (2026-10-13)."""
    calls.routes += [("api.open-meteo.com", OUTAGE), ("items/on-19", _fixture("eccc_item.json")),
                     ("properties=identifier", _far_listing(80))]
    lines = w.forecast({"latitude": 44.49, "longitude": -81.38, "days": 1}).splitlines()
    assert lines[1] == ("Environment Canada forecast for Port Elgin (Saugeen Shores - Kincardine - Southern "
                        "Bruce County; nearest city page is 80 km away, so a regional guide for this point, "
                        "not a local forecast), issued Tue 2026-10-13 05:00 EDT.")
    def first(needle):
        return next(n for n, url in enumerate(calls) if needle in url)
    assert first("api.weather.gov/points") < first("items/on-19"), "the NWS is asked first"
    # Within 40 km the page is local, and the NWS is never asked.
    calls.clear()
    w._ECCC_NEAREST.clear(), w._ECCC_PAGES.clear()
    calls.routes[-1] = ("properties=identifier", _far_listing(30))
    lines = w.forecast({"latitude": 44.49, "longitude": -81.38, "days": 1}).splitlines()
    assert "; nearest city page, 30 km away), " in lines[1]
    # (The alerts line asks the NWS afterwards, for every point.)
    assert first("items/on-19") < first("api.weather.gov/points"), "a near page needs no NWS"
    # Past 100 km, nothing.
    w._ECCC_NEAREST.clear(), w._ECCC_PAGES.clear()
    calls.routes[-1] = ("properties=identifier", _far_listing(110))
    with pytest.raises(w.ToolError, match="no fallback covers this point"):
        w.forecast({"latitude": 44.49, "longitude": -81.38, "days": 1})


def test_a_us_point_near_the_border_keeps_the_nws_over_a_far_canadian_page(calls):
    calls.routes += [("api.open-meteo.com", OUTAGE), ("properties=identifier", _far_listing(80, 46.5, -84.35)),
                     ("api.weather.gov/points/46.5000,-84.3500", _fixture("nws_points_soo.json")),
                     ("api.weather.gov/gridpoints/APX/65,131/forecast?units=si", _fixture("nws_forecast_soo.json"))]
    lines = w.forecast({"latitude": 46.5, "longitude": -84.35, "days": 1}).splitlines()
    assert lines[1].startswith("US National Weather Service forecast near Sault Ste. Marie, MI")


def test_a_route_fallback_stops_asking_once_its_time_budget_is_spent(calls, monkeypatch):
    ticks = iter([0, 0, w.FALLBACK_BUDGET_SECONDS + 1, w.FALLBACK_BUDGET_SECONDS + 2])
    monkeypatch.setattr(w, "_clock", lambda: next(ticks))
    _eccc_routes(calls)
    out = w.route_forecast({"stops": [
        {"latitude": 44.49, "longitude": -81.38, "date": "2026-10-14"},
        {"latitude": 45.0, "longitude": -80.0, "date": "2026-10-14"},
    ]})
    assert "1. 44.49, -81.38: Wed 2026-10-14 day: " in out
    assert "2. 45, -80: unavailable (fallback time budget spent)." in out


def test_a_route_asks_only_for_the_days_its_stops_need(calls):
    calls.routes.append(("api.open-meteo.com", _fixture("forecast_single.json")))
    w.route_forecast({"stops": [{"latitude": 44.49, "longitude": -81.38, "date": "2026-10-15"}]})
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(calls[0]).query)
    assert query["forecast_days"] == ["4"] and "hourly" in query


def test_forecast_asks_for_its_days_and_the_last_night_s_morning(calls):
    calls.routes.append(("api.open-meteo.com", _fixture("forecast_single.json")))
    w.forecast({"latitude": 44.49, "longitude": -81.38, "days": 3})
    w.forecast({"latitude": 44.49, "longitude": -81.38, "days": 16})
    sizes = [urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["forecast_days"] for url in calls
             if "open-meteo" in url]
    assert sizes == [["4"], ["16"]]


def test_the_eccc_search_box_holds_the_radius_in_the_north(calls):
    calls.routes += [("api.open-meteo.com", OUTAGE), ("api.weather.gc.ca", {"features": []}),
                     ("api.weather.gov/points", w._HTTPStatus("404", 404))]
    with pytest.raises(w.ToolError):
        w.forecast({"latitude": 70.0, "longitude": -100.0})
    west, south, east, north = map(float, urllib.parse.parse_qs(
        urllib.parse.urlsplit(calls[1]).query)["bbox"][0].split(","))
    assert w._km(70.0, -100.0, 70.0, east) >= w.ECCC_FAR_KM
    assert w._km(70.0, -100.0, north, -100.0) >= w.ECCC_FAR_KM


@pytest.mark.parametrize("province, lat, lon, zone", [
    ("on", 48.38, -89.25, "America/Toronto"),        # Thunder Bay
    ("on", 49.77, -94.49, "America/Winnipeg"),       # Kenora
    ("bc", 56.25, -120.85, "America/Dawson_Creek"),  # Fort St John
    ("bc", 49.28, -123.12, "America/Vancouver"),
    ("qc", 47.38, -61.86, "America/Halifax"),        # Magdalen Islands
    ("nu", 69.12, -105.06, "America/Cambridge_Bay"),
    ("nu", 62.81, -92.09, "America/Rankin_Inlet"),
    ("nu", 63.75, -68.52, "America/Iqaluit"),
])
def test_canadian_zones(province, lat, lon, zone):
    assert w._canadian_zone(province, lat, lon) == zone


def test_route_limits_and_numbered_errors(calls):
    stop = {"latitude": 44.49, "longitude": -81.38, "date": "2026-10-15"}
    with pytest.raises(w.ToolError, match="at most 12 stops"):
        w.route_forecast({"stops": [stop] * 13})
    with pytest.raises(w.ToolError, match="stop 2: date must be a date"):
        w.route_forecast({"stops": [stop, {**stop, "date": "Friday"}]})
    assert calls == []


# --- geocode --------------------------------------------------------------------

def test_geocode_lists_matches_with_coordinates_and_zone(calls):
    calls.routes.append(("geocoding-api.open-meteo.com", _fixture("geocode_wawa.json")))
    out = w.geocode({"name": "Wawa", "country_code": "ca"})
    assert "countryCode=CA" in calls[0]
    assert "- Wawa, Ontario, Canada (Algoma): 47.99, -84.77 · America/Toronto · pop 2,905" in out


def test_geocode_no_match_and_bad_country(calls):
    calls.routes.append(("geocoding-api.open-meteo.com", _fixture("geocode_empty.json")))
    assert w.geocode({"name": "Zzqxq"}) == "No place matched 'Zzqxq'."
    with pytest.raises(w.ToolError, match="two letters"):
        w.geocode({"name": "Wawa", "country_code": "CAN"})


# --- policy lockstep ----------------------------------------------------------------

def test_every_weather_tool_is_vetted_and_nothing_else_is():
    vetted = {t for t in boundaries.VETTED_PATHLESS_TOOLS if t.startswith("mcp__weather__")}
    assert vetted == {f"mcp__weather__{name}" for name in w.TOOLS}


def test_every_host_the_server_calls_is_in_the_egress_allowlist():
    source = SCRIPT.read_text()
    hosts = {urllib.parse.urlsplit(url).hostname for url in
             __import__("re").findall(r'"(https://[^"/]+)', source)}
    assert hosts and hosts <= set(boundaries.EGRESS_ALLOWLIST), hosts - set(boundaries.EGRESS_ALLOWLIST)


# --- 2026-10-13 follow-up: labels, snow, alerts, windows -------------------------------

def test_hourly_rows_name_the_hour_their_rain_fell_in_and_snow_is_said_when_there_is_some(calls):
    payload = _fixture("forecast_single.json")
    hours = payload["hourly"]["time"]
    payload["hourly"]["snowfall_gem_seamless"] = [None] * len(hours)
    payload["hourly"]["snowfall_gem_seamless"][hours.index("2026-10-14T03:00")] = 0.7
    payload["daily"]["snowfall_sum_gem_seamless"] = [0.0] * len(payload["daily"]["time"])
    payload["daily"]["snowfall_sum_gem_seamless"][7] = 1.4
    calls.routes.append(("api.open-meteo.com", payload))
    out = w.forecast({"latitude": 44.49, "longitude": -81.38, "days": 8, "hours": "tomorrow"})
    # 03:00 Wednesday is Tuesday's night; the day after it is dry of snow.
    assert "% chance, 0.7 cm snow, wind" in next(l for l in out.splitlines() if l.startswith("Tue 2026-10-13 night"))
    assert "cm snow" not in next(l for l in out.splitlines() if l.startswith("Wed 2026-10-14 day"))
    assert "% chance, 1.4 cm snow, wind" in next(l for l in out.splitlines() if l.startswith("Tue 2026-10-20: "))
    hour = next(l for l in out.splitlines() if l.startswith("03:00 EDT: "))
    assert " km/h; 02:00–03:00: " in hour and " mm, 1% chance, 0.7 cm snow [" in hour


def test_a_us_forecast_states_nws_alerts(calls):
    calls.routes += [("api.open-meteo.com", _fixture("forecast_single.json")),
                     ("api.weather.gov/points/46.5000,-84.3500", _fixture("nws_points_soo.json")),
                     ("api.weather.gov/alerts/active?point=46.5000,-84.3500", {"features": [
                         {"properties": {"event": "Gale Warning", "severity": "Moderate",
                                         "ends": "2026-10-15T06:00:00-04:00"}},
                         {"properties": {"event": "Gale Warning", "severity": "Moderate",
                                         "ends": "2026-10-15T06:00:00-04:00"}},
                     ]})]
    out = w.forecast({"latitude": 46.5, "longitude": -84.35, "days": 1})
    assert ("ALERTS IN EFFECT (US National Weather Service): "
            "Gale Warning (moderate), until Thu 2026-10-15 06:00 (UTC-0400).") in out


def test_failing_to_check_alerts_costs_the_line_not_the_forecast(calls):
    calls.routes += [("api.open-meteo.com", _fixture("forecast_single.json")),
                     ("api.weather.gc.ca", w._Transient("api.weather.gc.ca unreachable (URLError)", 0))]
    lines = w.forecast({"latitude": 44.49, "longitude": -81.38, "days": 1}).splitlines()
    assert lines[2] == "Alerts: could not check (api.weather.gc.ca unreachable (URLError))."
    assert lines[3].startswith("Tue 2026-10-13 day ")


def test_alerts_elsewhere_say_they_were_not_checked(calls):
    assert w._alerts(48.85, 2.35) == "Alerts: not checked (no alert source for this region)."


def _expected(fixture, first, last, var):
    hourly = fixture["hourly"]
    return [v for t, v in zip(hourly["time"], hourly[f"{var}_gem_seamless"]) if first <= t <= last and v is not None]


def test_the_old_window_arguments_are_gone_from_the_schema():
    stop = w.TOOLS["route_forecast"][1]["inputSchema"]["properties"]["stops"]["items"]["properties"]
    assert "from_hour" not in stop and "to_hour" not in stop
    assert w.TOOLS["forecast"][1]["inputSchema"]["properties"]["hours"]["enum"] == ["today", "tomorrow"]


def test_route_alerts_are_checked_once_per_point_within_the_budget(calls, monkeypatch):
    import time

    monkeypatch.setattr(w, "ALERT_BUDGET_SECONDS", 0.3)
    payload = _multi()
    payload.insert(1, copy.deepcopy(payload[0]))
    calls.routes += [
        ("api.open-meteo.com", payload),
        # The Soo's lookup hangs past the budget; Port Elgin's answers at once.
        ("weather-alerts/items?f=json&lang=en&limit=100&bbox=-84.3600", lambda: time.sleep(1.0) or {"features": []}),
    ]
    lines = w.route_forecast({"stops": [
        {"latitude": 44.49, "longitude": -81.38, "date": "2026-10-14"},
        {"latitude": 44.49, "longitude": -81.38, "date": "2026-10-15"},
        {"latitude": 46.52, "longitude": -84.35, "date": "2026-10-15"},
    ]}).splitlines()
    alerts = [l for l in lines if l.startswith("   Alerts")]
    assert alerts == ["   Alerts: not checked (no alert source for this region)."] * 2 + [
        "   Alerts: not checked (time budget spent)."]
    assert sum("bbox=-81.3900,44.4800" in url for url in calls) == 1, "the repeated point is looked up once"


def test_alerts_are_one_quick_attempt_not_a_retried_timeout(monkeypatch):
    attempts, slept = [], []

    def down(url, timeout=None):
        attempts.append(timeout)
        raise w._Transient("api.weather.gc.ca unreachable (TimeoutError)", 0)

    monkeypatch.setattr(w, "_fetch_once", down)
    monkeypatch.setattr(w, "_sleep", slept.append)
    w._ECCC_PAGES.clear()
    w._ECCC_NEAREST.clear()
    assert w._alerts(44.49, -81.38) == "Alerts: could not check (api.weather.gc.ca unreachable (TimeoutError))."
    assert attempts == [w.ALERT_TIMEOUT_SECONDS] and slept == []


def test_a_stop_beyond_the_forecast_still_gets_its_alerts(calls):
    calls.routes.append(("api.open-meteo.com", _fixture("forecast_single.json")))
    lines = w.route_forecast({"stops": [
        {"latitude": 44.49, "longitude": -81.38, "date": "2026-11-30"}]}).splitlines()
    assert lines[2].endswith("is beyond the forecast (it ends 2026-10-28; for dates that far out, "
                             "typical conditions for the month are the best guide).")
    assert lines[3].startswith("   Alerts: ")


def test_a_period_across_the_clock_change_names_both_zones():
    tz = w.ZoneInfo("America/Toronto")
    assert w._zone_span(tz, "2026-10-31T18:00", "2026-11-01T08:00") == "EDT→EST"
    assert w._zone_span(tz, "2026-10-14T18:00", "2026-10-15T08:00") == "EDT"


def test_a_value_that_rounds_to_zero_has_no_minus_sign():
    assert [w._fmt(-0.4), w._fmt(-0.04, " mm", 1), w._fmt(-1.6), w._fmt(0.0, " mm", 1)] == [
        "0", "0.0 mm", "-2", "0.0 mm"]


# --- Canadian alerts from Environment Canada's alert polygons ---------------------------

def _square(lat, lon, half=0.2):
    ring = [[lon - half, lat - half], [lon + half, lat - half], [lon + half, lat + half],
            [lon - half, lat + half], [lon - half, lat - half]]
    return {"type": "Polygon", "coordinates": [ring]}


def _alert(lat, lon, **props):
    feature = copy.deepcopy(_fixture("eccc_alerts.json")["features"][0])
    feature["geometry"] = _square(lat, lon)
    feature["properties"].update({
        "alert_name_en": "wind warning", "feature_name_en": "Sault Ste. Marie - Superior East",
        "status_en": "issued", "province": "ON",
        "expiration_datetime": "2026-10-14T22:00:00.000Z",
        "alert_text_en": "Strong winds expected tonight\n\nWhat: gusts to 90 km/h", **props})
    return feature


def test_an_alert_whose_area_contains_the_point_is_stated(calls):
    calls.routes += [("api.open-meteo.com", _fixture("forecast_single.json")),
                     ("collections/weather-alerts", {"features": [_alert(44.49, -81.38)]})]
    lines = w.forecast({"latitude": 44.49, "longitude": -81.38, "days": 1}).splitlines()
    assert lines[2] == ("ALERTS IN EFFECT (Environment Canada, at this point): Wind warning for "
                        "Sault Ste. Marie - Superior East, until Wed 2026-10-14 18:00 EDT "
                        "(Strong winds expected tonight).")
    assert not any("citypageweather" in url for url in calls), "an alert settles Canada by itself"


def test_an_alert_area_beside_the_point_is_not_its_alert(calls):
    """Marlow, 2026-10-13: Chutes was given Gore Bay's page, 44 km across the
    water, a different warning area. The point must be inside the polygon."""
    neighbour = _alert(45.9, -82.47)  # Manitoulin, ~40 km away
    calls.routes += [("collections/weather-alerts", {"features": [neighbour]}),
                     ("properties=identifier", _fixture("eccc_list.json"))]
    assert w._alerts(44.49, -81.38) == "Alerts: none in effect (Environment Canada, at this point)."


@pytest.mark.parametrize("props", [
    {"status_en": "ended"},
    {"status_en": "cancelled"},
    {"expiration_datetime": "2026-10-13T10:00:00.000Z"},  # before NOW (12:30Z)
])
def test_ended_or_expired_alerts_are_not_in_effect(calls, props):
    calls.routes += [("collections/weather-alerts", {"features": [_alert(44.49, -81.38, **props)]}),
                     ("properties=identifier", _fixture("eccc_list.json"))]
    assert w._alerts(44.49, -81.38) == "Alerts: none in effect (Environment Canada, at this point)."


def test_a_remote_canadian_point_is_never_given_a_us_all_clear(calls):
    """Agawa Bay is ~50 km from any city page (review, 2026-10-13). With no
    alert there, NWS rules itself out (404) and the 150 km listing says Canada."""
    listing = _fixture("eccc_list.json")
    listing["features"] = [f for f in listing["features"] if f["id"] == "on-19"]  # Port Elgin only
    calls.routes.append(("properties=identifier", listing))
    # 0.5° north of Port Elgin: ~45 km, outside the 40 km radius.
    assert w._alerts(44.84, -81.40) == "Alerts: none in effect (Environment Canada, at this point)."
    assert any("api.weather.gov/points" in url for url in calls), "the US was ruled out first"
    assert not any("alerts/active" in url for url in calls)


def test_a_failed_canadian_alert_lookup_says_so_not_none(calls):
    calls.routes.append(("collections/weather-alerts", w._Transient("api.weather.gc.ca returned HTTP 503", 503)))
    assert w._alerts(44.49, -81.38) == "Alerts: could not check (api.weather.gc.ca returned HTTP 503)."


def test_point_in_polygon_handles_multipolygons_and_holes():
    outer = _square(45.0, -80.0, 1.0)["coordinates"][0]
    hole = _square(45.0, -80.0, 0.2)["coordinates"][0]
    elsewhere = _square(50.0, -100.0, 1.0)["coordinates"]
    holed = {"type": "Polygon", "coordinates": [outer, hole]}
    multi = {"type": "MultiPolygon", "coordinates": [elsewhere, [outer, hole]]}
    for geometry in (holed, multi):
        assert w._contains(geometry, 45.5, -80.5)
        assert not w._contains(geometry, 45.0, -80.0), "inside the hole"
        assert not w._contains(geometry, 47.0, -80.0)
    assert w._contains(multi, 50.0, -100.0)


def test_a_us_border_city_is_asked_of_nws_not_the_nearby_canadian_page(calls):
    """Live check, 2026-10-13: Detroit is within 40 km of Windsor's page and
    was told 'none (Environment Canada)' without NWS being asked."""
    calls.routes += [("api.weather.gov/points/42.3300,-83.0500", _fixture("nws_points_soo.json")),
                     ("api.weather.gov/alerts/active?point=42.3300,-83.0500", {"features": [
                         {"properties": {"event": "Wind Advisory", "severity": "Moderate"}}]}),
                     ("properties=identifier", _fixture("eccc_list.json"))]
    assert w._alerts(42.33, -83.05) == "ALERTS IN EFFECT (US National Weather Service): Wind Advisory (moderate)."


# --- the problem log: other people's services change (Mike, 2026-10-13) ----------------

def _logged(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "marlow.weather"]


def test_a_failed_fetch_is_logged_without_the_query_or_a_precise_position(monkeypatch, caplog):
    def down(url, timeout=None):
        raise w._HTTPStatus("api.weather.gc.ca returned HTTP 404", 404)

    monkeypatch.setattr(w, "_fetch_once", down)
    with pytest.raises(w.ToolError):
        w._fetch_json("https://api.weather.gc.ca/collections/weather-alerts/items?bbox=-81.38,44.49")
    with pytest.raises(w.ToolError):
        w._fetch_json("https://api.met.no/x/44.49,-81.38")
    assert _logged(caplog) == [
        "FETCH api.weather.gc.ca/collections/weather-alerts/items failed (1 attempt): "
        "api.weather.gc.ca returned HTTP 404",
        "FETCH api.met.no/x/44.4,-81.3 failed (1 attempt): api.weather.gc.ca returned HTTP 404"]


def test_the_nws_saying_not_the_us_is_an_answer_not_a_problem(monkeypatch, caplog):
    monkeypatch.setattr(w, "_fetch_once", lambda url, timeout=None: (_ for _ in ()).throw(
        w._HTTPStatus("api.weather.gov returned HTTP 404", 404)))
    assert w._nws_covers(46.52, -84.35) is False
    assert _logged(caplog) == []


def test_an_alert_feed_that_renamed_its_name_field_still_warns_and_logs(calls, caplog):
    feature = _alert(44.49, -81.38)
    feature["properties"]["alert_title_en"] = feature["properties"].pop("alert_name_en")
    calls.routes.append(("collections/weather-alerts", {"features": [feature]}))
    line = w._alerts(44.49, -81.38)
    assert line.startswith("ALERTS IN EFFECT (Environment Canada, at this point): Unnamed alert (feed format changed)")
    assert any(m.startswith("DRIFT eccc alerts: an alert at 44.5,-81.4 has no alert_name_en; keys: ")
               and "alert_title_en" in m for m in _logged(caplog))


def test_an_alert_area_that_is_not_a_polygon_is_logged(calls, caplog):
    feature = _alert(44.49, -81.38)
    feature["geometry"] = {"type": "GeometryCollection", "geometries": []}
    calls.routes.append(("collections/weather-alerts", {"features": [feature]}))
    w._alerts(44.49, -81.38)
    assert "DRIFT eccc alerts: alert area of type 'GeometryCollection', not a polygon" in _logged(caplog)


def test_an_alert_status_nobody_has_seen_counts_as_in_effect_and_is_logged(calls, caplog):
    calls.routes.append(("collections/weather-alerts", {"features": [_alert(44.49, -81.38, status_en="amended")]}))
    assert w._alerts(44.49, -81.38).startswith("ALERTS IN EFFECT")
    assert "DRIFT eccc alerts: unrecognised status_en 'amended' (counted as in effect)" in _logged(caplog)


def test_an_alert_feed_without_features_is_could_not_check_never_none(calls, caplog):
    calls.routes.append(("collections/weather-alerts", {"type": "FeatureCollection", "items": []}))
    assert w._alerts(44.49, -81.38).startswith("Alerts: could not check (")
    assert "DRIFT eccc alerts: response has no features list" in _logged(caplog)


def test_a_city_page_whose_periods_no_longer_parse_is_logged(calls, caplog):
    item = _fixture("eccc_item.json")
    for period in item["properties"]["forecastGroup"]["forecasts"]:
        period["period"]["value"] = {"en": "Day 1", "fr": "Jour 1"}
    _eccc_routes(calls, item)
    w.forecast({"latitude": 44.49, "longitude": -81.38, "days": 2})
    messages = _logged(caplog)
    assert any(m.startswith("DRIFT eccc city page: on-19: no dated periods in ") for m in messages)
    assert any(m.startswith("FALLBACK used at 44.5,-81.4: Environment Canada forecast for Port Elgin") for m in messages)


def test_a_fallback_of_the_wrong_shape_is_logged_with_its_traceback(calls, caplog):
    item = _fixture("eccc_item.json")
    del item["properties"]["forecastGroup"]
    _eccc_routes(calls, item)
    with pytest.raises(w.ToolError, match="unexpected shape"):
        w.forecast({"latitude": 44.49, "longitude": -81.38})
    record = next(r for r in caplog.records if r.getMessage().startswith("SHAPE fallback at 44.5,-81.4: KeyError"))
    assert record.exc_info is not None, "the traceback names the line that broke"


def test_an_open_meteo_answer_without_hours_is_logged_and_still_answers(calls, caplog):
    payload = _fixture("forecast_single.json")
    del payload["hourly"]
    calls.routes.append(("api.open-meteo.com", payload))
    rows = w.forecast({"latitude": 44.49, "longitude": -81.38, "days": 2}).splitlines()[3:]
    assert [r[:16] for r in rows] == ["Tue 2026-10-13: ", "Wed 2026-10-14: "]
    assert any(m.startswith("DRIFT open-meteo: no day/night periods for 44.5,-81.4") for m in _logged(caplog))


def test_an_internal_error_is_logged_with_its_traceback(monkeypatch, caplog):
    monkeypatch.setitem(w.TOOLS, "forecast", (lambda _args: 1 / 0, w.TOOLS["forecast"][1]))
    w.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "forecast", "arguments": {}}})
    record = next(r for r in caplog.records if r.name == "marlow.weather")
    assert record.getMessage() == "INTERNAL forecast: ZeroDivisionError: division by zero" and record.exc_info


def test_the_log_flag_appends_to_its_file_and_a_bad_path_costs_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(w.log, "handlers", [])
    monkeypatch.setattr(w.log, "propagate", True)
    path = tmp_path / "weather.log"
    w._log_to(["--log", str(path)])
    w._drift("test", "a line")
    for handler in w.log.handlers:
        handler.close()
    assert path.read_text().rstrip().endswith("WARNING [marlow.weather] DRIFT test: a line")
    w._log_to(["--log", str(tmp_path / "missing" / "weather.log")])  # no raise


# --- /code-review high on the periods and the log (2026-10-13) ---------------------------

def test_quebec_city_pages_with_letter_ids_are_used(calls, caplog):
    listing = _fixture("eccc_list.json")
    for feature in listing["features"]:
        if feature["id"] == "on-19":
            feature["id"] = "qc-a0"  # real shape: qc-a0..qc-d4 on the live listing
    calls.routes += [("properties=identifier", listing)]
    assert w._eccc_nearest(44.49, -81.38, 40)[1] == "qc-a0"
    assert not any(m.startswith("DRIFT") for m in _logged(caplog))


def test_a_route_without_hourly_data_still_answers_and_logs(calls, caplog):
    payload = _multi()[0]
    del payload["hourly"]
    calls.routes.append(("api.open-meteo.com", payload))
    lines = w.route_forecast({"stops": [{"latitude": 44.49, "longitude": -81.38, "date": "2026-10-15"}]}).splitlines()
    assert lines[2] == "1. 44.49, -81.38:" and lines[3].startswith("   Thu 2026-10-15: ")
    assert any(m.startswith("DRIFT open-meteo: no day/night periods") for m in _logged(caplog))


def test_period_rain_is_the_hours_ending_inside_it():
    # The value at T is the hour ending at T: 06:00's is 05-06 (night),
    # 18:00's is 17-18 (day).
    times = [f"2026-10-14T{h:02d}:00" for h in range(24)]
    rain = [0.0] * 24
    rain[6], rain[18] = 5.0, 2.0
    hourly = {"time": times, "temperature_2m_gem_seamless": [5.0] * 24, "precipitation_gem_seamless": rain}
    line = w._period_line(hourly, w.ZoneInfo("America/Toronto"), w.date(2026, 10, 14), False, "2026-10-14T00:00")
    assert ", 2.0 mm total (heaviest 2.0 mm 17:00–18:00), " in line
    rain[18], rain[13] = 0.04, 1.5
    line = w._period_line(hourly, w.ZoneInfo("America/Toronto"), w.date(2026, 10, 14), False, "2026-10-14T00:00")
    assert ", 1.5 mm total (heaviest 1.5 mm 12:00–13:00), " in line
    rain[13] = 0.0
    line = w._period_line(hourly, w.ZoneInfo("America/Toronto"), w.date(2026, 10, 14), False, "2026-10-14T00:00")
    assert ", 0.0 mm total, " in line, "a trace too small to print has no heaviest hour"


def test_a_leftover_night_does_not_use_up_a_fallback_day(calls, monkeypatch):
    monkeypatch.setattr(w, "_now", lambda: datetime(2026, 10, 14, 1, 0, tzinfo=timezone.utc))
    payload = _fixture("metno_london.json")
    payload["properties"]["timeseries"] = [s for s in payload["properties"]["timeseries"]
                                           if s["time"] >= "2026-10-14T01:00:00Z"]
    calls.routes += [("api.open-meteo.com", OUTAGE), ("api.weather.gc.ca", {"features": []}),
                     ("api.met.no", payload)]
    rows = w.forecast({"latitude": 51.51, "longitude": -0.13, "days": 1}).splitlines()[3:]
    assert [r.split(":")[0].split(" (")[0] for r in rows] == [
        "Tue 2026-10-13 night", "Wed 2026-10-14 day", "Wed 2026-10-14 night"]


def test_an_arrival_hour_missing_from_the_data_says_so(calls):
    payload = _multi()[0]
    at = payload["hourly"]["time"].index("2026-10-14T14:00")
    payload["hourly"]["time"][at] = "2026-10-14T14:30"
    calls.routes.append(("api.open-meteo.com", payload))
    out = w.route_forecast({"stops": [{"latitude": 44.49, "longitude": -81.38, "date": "2026-10-14", "hour": 14}]})
    assert "   At 14:00: not in the hourly data." in out
