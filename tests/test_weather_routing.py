"""Routing mode of route_forecast (docs/tasks/route-planning.md).

No network: OSRM answers come from tests/fixtures/weather/osrm_*.json,
recorded from router.project-osrm.org on 2026-10-14 (step geometry and
waypoint hints stripped, annotations to 0.1). Town names come from the real
server/data/places.tsv.gz, which is part of what is under test.
"""
from __future__ import annotations

import copy
import io
import json
import re
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import pytest

from server import weather_mcp as w

FIXTURES = Path(__file__).parent / "fixtures" / "weather"
NOW = datetime(2026, 10, 13, 12, 30, tzinfo=timezone.utc)
CALGARY = {"latitude": 51.04, "longitude": -114.07, "label": "Calgary"}
EDMONTON = {"latitude": 53.55, "longitude": -113.49, "label": "Edmonton"}
CABO = {"latitude": 22.89, "longitude": -109.92, "label": "Cabo San Lucas"}
SOO = {"latitude": 46.52, "longitude": -84.33, "label": "Sault Ste. Marie"}
MARATHON = {"latitude": 48.72, "longitude": -86.38, "label": "Marathon"}
THUNDER_BAY = {"latitude": 48.38, "longitude": -89.25, "label": "Thunder Bay"}
VANCOUVER = {"latitude": 49.28, "longitude": -123.12, "label": "Vancouver"}
VICTORIA = {"latitude": 48.43, "longitude": -123.37, "label": "Victoria"}
NEAR_MARATHON = {"latitude": 48.7, "longitude": -86.2, "label": "Marathon"}
CALGARY_TOO = {"latitude": 51.05, "longitude": -114.07, "label": "Calgary"}


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


def _weather(url: str):
    """Open-Meteo for however many points were asked: the single-point
    recording (Port Elgin, 2026-10-13 to 10-28) at each, in its own zone."""
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    lats = query["latitude"][0].split(",")
    lons = query["longitude"][0].split(",")
    one = _fixture("forecast_single.json")
    points = []
    for lat, lon in zip(lats, lons):
        point = copy.deepcopy(one)
        point["latitude"], point["longitude"] = float(lat), float(lon)
        point["timezone"] = w._tz_at(float(lat), float(lon)).key
        points.append(point)
    return points if len(points) > 1 else points[0]


class _Calls(list):
    """Every URL fetched, in order; `routes` holds the OSRM answer."""

    def __init__(self) -> None:
        super().__init__()
        self.routes: dict[str, object] = {}


@pytest.fixture
def calls(monkeypatch):
    seen = _Calls()
    routes = seen.routes

    def fake(url: str, **_options):
        seen.append(url)
        if "router.project-osrm.org" in url:
            answer = routes["osrm"]
            if isinstance(answer, Exception):
                raise answer
            return copy.deepcopy(answer)
        if "api.open-meteo.com" in url:
            answer = routes.get("weather", _weather)
            if isinstance(answer, Exception):
                raise answer
            return answer(url)
        if "api.weather.gov/points" in url:
            raise w._HTTPStatus("api.weather.gov returned HTTP 404", 404)
        if "api.weather.gc.ca" in url or "api.weather.gov/alerts" in url:
            return {"features": []}
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(w, "_fetch_json", fake)
    monkeypatch.setattr(w, "_now", lambda: NOW)
    monkeypatch.setattr(w, "_sleep", lambda _s: None)
    w._ECCC_PAGES.clear()
    w._ECCC_NEAREST.clear()
    w._OSRM_LAST.clear()
    return seen


def _route(calls, fixture: str, stops: list[dict], **args) -> str:
    calls.routes["osrm"] = _fixture(fixture)
    return w.route_forecast({"stops": stops, **args})


def _days(out: str) -> list[str]:
    return [line for line in out.splitlines() if re.match(r"Day [0-9]+, ", line)]


def _osrm_calls(calls) -> list[str]:
    return [url for url in calls if "router.project-osrm.org" in url]


# --- the long trip: days, overnights, towns -------------------------------------

def test_calgary_to_cabo_splits_into_days_at_the_cap(calls):
    out = _route(calls, "osrm_cabo.json", [CALGARY, CABO], depart="2026-10-15T10:00")
    assert out.startswith("Route: Calgary → Cabo San Lucas, 4,095 km, 50.3 h of driving over 8 days. ")
    days = _days(out)
    assert len(days) == 8
    assert days[0] == ("Day 1, Thu 15 Oct: Calgary 10:00 MDT → Helena, MT 17:11 MDT (suggested overnight), "
                       "642 km, 7.2 h")
    assert days[-1].startswith("Day 8, Thu 22 Oct: Ciudad Constitución, BCS 10:00 MST → Cabo San Lucas 14:37 MST")
    hours = [float(re.search(r", ([0-9.]+) h$", d).group(1)) for d in days]
    assert max(hours) <= 8.0
    assert sum(hours) == pytest.approx(50.3, abs=0.2)
    # One request carries every stop, rounded as Open-Meteo's are.
    (url,) = _osrm_calls(calls)
    assert "/route/v1/driving/-114.07,51.04;-109.92,22.89?" in url
    assert "geometries=polyline6" in url and "annotations=duration,distance" in url


def test_a_suggested_overnight_is_the_largest_town_in_the_last_hour(calls):
    # The last town before day 1's cap is Boulder, MT; Helena is the one a
    # person stops in. Day 2's last is Vineyard, a suburb of Salt Lake.
    days = _days(_route(calls, "osrm_cabo.json", [CALGARY, CABO], depart="2026-10-15T10:00"))
    assert "→ Helena, MT" in days[0] and "→ Salt Lake City, UT" in days[1]


def _options(out: str) -> list[str]:
    """Each day's options line, or "" for a day without one."""
    days = re.split(r"\n(?=Day [0-9]+, )", out.split("\nWeather")[0])[1:]
    return [next((line for line in day.splitlines() if line.startswith("   overnight options: ")), "")
            for day in days]


def test_a_suggested_overnight_lists_the_bigger_towns_before_the_cap(calls):
    # Marlow's Marathon → Calgary at 6.5 h (2026-10-14): the pick is a hamlet
    # past Thunder Bay, which sits outside the last hour.
    out = _route(calls, "osrm_calgary.json", [NEAR_MARATHON, CALGARY_TOO], depart="2026-10-15T08:00",
                 max_drive_hours=6.5)
    assert "→ Raith, ON 13:40 EDT (suggested overnight)" in _days(out)[0]
    assert _options(out)[0] == "   overnight options: Thunder Bay, ON 4.5 h (109k) · Raith, ON 5.7 h (small)"
    assert "its options line lists the larger towns in the 2 h before the cap" in out


def test_overnight_options_skip_suburbs_but_not_a_city_beside_the_pick(calls):
    out = _route(calls, "osrm_cabo.json", [CALGARY, CABO], depart="2026-10-15T10:00")
    options = _options(out)
    # Salt Lake's suburbs (West Valley City, West Jordan) are one choice.
    assert options[1] == ("   overnight options: Tremonton, UT 6.5 h (8.2k) · Ogden, UT 7.1 h (85k) · "
                          "Salt Lake City, UT 7.7 h (216k)")
    # Nothing but Helena's own suburbs to choose from: no line.
    assert options[0] == ""
    out = _route(calls, "osrm_cabo.json", [CALGARY, CABO], depart="2026-10-15T10:00", max_drive_hours=6.5)
    assert "   overnight options: Las Vegas, NV 4.7 h (642k) · Henderson, NV 4.9 h (286k)" in _options(out)


def test_osrm_is_asked_at_most_once_a_second(calls, monkeypatch):
    now, slept = [100.0], []
    monkeypatch.setattr(w, "_clock", lambda: now[0])
    monkeypatch.setattr(w, "_sleep", lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)))
    _route(calls, "osrm_edmonton.json", [CALGARY, EDMONTON], depart="2026-10-20T09:00")
    assert slept == []
    now[0] += 0.25
    _route(calls, "osrm_edmonton.json", [CALGARY, EDMONTON], depart="2026-10-20T09:00")
    assert slept == [pytest.approx(0.75)]
    now[0] += 5
    _route(calls, "osrm_edmonton.json", [CALGARY, EDMONTON], depart="2026-10-20T09:00")
    assert slept == [pytest.approx(0.75)]


def test_the_routing_part_of_an_eight_day_trip_stays_under_three_thousand_characters(calls):
    # ~2.1K, then ~2.6K with the overnight options (2026-10-14).
    out = _route(calls, "osrm_cabo.json", [CALGARY, CABO], depart="2026-10-15T10:00")
    assert len(out.split("\nWeather at ")[0]) < 3000


def test_borders_are_named_and_passing_a_far_shore_is_not_a_crossing(calls):
    out = _route(calls, "osrm_cabo.json", [CALGARY, CABO], depart="2026-10-15T10:00")
    assert "Border crossings: Coutts, AB → Shelby, MT; Calexico, CA → Mexicali, BC." in out
    # The ferry passes Point Roberts, WA, which is the nearest town for a few km.
    out = _route(calls, "osrm_victoria.json", [VANCOUVER, VICTORIA], depart="2026-11-20T09:00")
    assert "Border crossings" not in out
    out = _route(calls, "osrm_thunderbay.json", [SOO, MARATHON, THUNDER_BAY], depart="2026-10-14T09:00")
    assert "Border crossings" not in out


def test_via_towns_are_one_an_hour_and_name_a_zone_where_it_changes(calls):
    days = _route(calls, "osrm_cabo.json", [CALGARY, CABO], depart="2026-10-15T10:00").splitlines()
    day3 = days[days.index(next(l for l in days if l.startswith("Day 3"))) + 1]
    assert "14:57 Saint George · 14:40 PDT Mesquite" in day3
    times = [int(t[:2]) * 60 + int(t[3:]) for t in re.findall(r"\b(\d\d:\d\d)\b", day3)]
    assert len(times) >= 5


def test_a_stretch_with_no_town_says_so(calls):
    out = _route(calls, "osrm_cabo.json", [CALGARY, CABO], depart="2026-10-15T10:00")
    assert "   Nothing named for 6.9 h after San Felipe, BC (to Guerrero Negro, BCS)." in out


def test_a_city_bypassed_by_the_highway_is_still_named(calls):
    # Red Deer's centre is ~5 km off the QE2.
    out = _route(calls, "osrm_edmonton.json", [CALGARY, EDMONTON], depart="2026-10-14T09:00")
    assert " Red Deer" in next(l for l in out.splitlines() if l.startswith("   via "))


def test_via_towns_minutes_apart_are_one_stretch(calls):
    via = next(l for l in _route(calls, "osrm_edmonton.json", [CALGARY, EDMONTON],
                                 depart="2026-10-14T09:00").splitlines() if l.startswith("   via "))
    times = [int(t[:2]) * 60 + int(t[3:]) for t in re.findall(r"\b(\d\d:\d\d)\b", via)]
    assert all(b - a >= 30 for a, b in zip(times, times[1:])), via


# --- the user's habits: start hour, cap, chosen overnights ------------------------

def test_the_default_start_and_cap_are_stated_as_defaults(calls):
    out = _route(calls, "osrm_cabo.json", [CALGARY, CABO], depart="2026-10-15T10:00")
    assert ("Each day after an overnight starts at 10:00 local (default; pass start_hour; an overnight stop's own "
            "start_hour overrides it) and ends by 8 h of driving (default; pass max_drive_hours).") in out
    assert all(" 10:00 " in d.split(" → ")[0] for d in _days(out))


def test_a_marked_overnight_ends_the_day_and_its_start_hour_wins(calls):
    stops = [SOO, {**MARATHON, "overnight": True, "start_hour": 7}, THUNDER_BAY]
    out = _route(calls, "osrm_thunderbay.json", stops, depart="2026-10-14T09:00", start_hour=6)
    days = _days(out)
    assert days[0].startswith("Day 1, Wed 14 Oct: Sault Ste. Marie 09:00 EDT → Marathon 14:49 EDT, ")
    assert days[1].startswith("Day 2, Thu 15 Oct: Marathon 07:00 EDT → Thunder Bay ")
    assert "starts at 06:00 local (as asked;" in out


def test_the_trip_start_hour_applies_after_a_suggested_overnight(calls):
    out = _route(calls, "osrm_cabo.json", [CALGARY, CABO], depart="2026-10-15T05:00", start_hour=5,
                 max_drive_hours=10)
    days = _days(out)
    assert all(" 05:00 " in d.split(" → ")[0] for d in days)
    assert "ends by 10 h of driving (as asked)" in out
    assert len(days) < 8


def test_an_unmarked_middle_stop_is_passed_not_slept_at(calls):
    out = _route(calls, "osrm_thunderbay.json", [SOO, MARATHON, THUNDER_BAY], depart="2026-10-14T09:00")
    assert len(_days(out)) == 2
    assert "Marathon (stop 2)" in out


def test_too_many_days_is_refused(calls):
    with pytest.raises(w.ToolError, match="more than 21 days of driving at 1 h a day"):
        _route(calls, "osrm_cabo.json", [CALGARY, CABO], depart="2026-10-15T10:00", max_drive_hours=1)


# --- weather, by how far off the trip is -------------------------------------------

def test_weather_is_asked_at_the_start_each_overnight_and_the_end_at_their_hours(calls):
    out = _route(calls, "osrm_thunderbay.json", [SOO, {**MARATHON, "overnight": True}, THUNDER_BAY],
                 depart="2026-10-14T09:00")
    (url,) = [u for u in calls if "api.open-meteo.com" in u]
    assert "latitude=46.52,48.72,48.38&" in url
    weather = out.split("\nWeather at ")[1]
    assert "1. Sault Ste. Marie (46.52, -84.33):" in weather and "At 09:00 EDT:" in weather
    assert "2. Marathon (48.72, -86.38):" in weather and "At 15:00 EDT:" in weather  # 14:49, nearest hour


def test_a_trip_past_the_forecast_gets_its_days_and_no_weather(calls):
    out = _route(calls, "osrm_cabo.json", [CALGARY, CABO], depart="2026-12-10T10:00")
    assert len(_days(out)) == 8
    assert out.endswith("Weather: the departure is beyond the 16-day forecast; for dates that far out, "
                        "typical conditions for the month are the best guide.")
    assert not [u for u in calls if "api.open-meteo.com" in u]


def test_a_drive_today_gets_weather_and_alerts_at_each_town_on_the_way(calls):
    # NOW is 06:30 MDT; leaving at 09:00, one driving day.
    out = _route(calls, "osrm_edmonton.json", [CALGARY, EDMONTON], depart="2026-10-13T09:00")
    passing = [l for l in out.splitlines() if ", passing at " in l]
    assert len(passing) >= 3 and any("Red Deer, AB" in l for l in passing)
    assert out.count("Alerts:") == len(passing) + 2


def test_a_multi_day_trip_does_not_get_weather_at_every_town(calls):
    # Leaving within the 24 hours, but eight days of driving.
    out = _route(calls, "osrm_cabo.json", [CALGARY, CABO], depart="2026-10-13T10:00")
    assert ", passing at " not in out


def test_a_one_day_trip_next_week_does_not_either(calls):
    out = _route(calls, "osrm_edmonton.json", [CALGARY, EDMONTON], depart="2026-10-20T09:00")
    assert ", passing at " not in out


# --- what the road says ------------------------------------------------------------

def test_ferries_are_flagged_with_their_crossing_time(calls):
    out = _route(calls, "osrm_victoria.json", [VANCOUVER, VICTORIA], depart="2026-11-20T09:00")
    ferries = [l for l in out.splitlines() if l.startswith("Ferry: ")]
    assert len(ferries) == 2
    assert ferries[0].endswith(" h crossing (OSRM: Berth 2 Approach).") and ", 1.5 h crossing" in ferries[0]


def test_a_stop_far_from_any_road_is_flagged(calls):
    payload = _fixture("osrm_thunderbay.json")
    payload["waypoints"][1]["distance"] = 14000.0
    calls.routes["osrm"] = payload
    out = w.route_forecast({"stops": [SOO, MARATHON, THUNDER_BAY], "depart": "2026-10-14T09:00"})
    assert "Stop 2 (Marathon) is 14 km from the nearest road;" in out
    assert "Stop 1 " not in out and "Stop 3 " not in out


def test_without_the_places_file_the_trip_is_still_timed(calls, monkeypatch):
    monkeypatch.setattr(w, "_PLACES", w._PlaceIndex())
    out = _route(calls, "osrm_cabo.json", [CALGARY, CABO], depart="2026-12-10T10:00")
    assert "Town names are unavailable" in out
    assert "→ the road at " in _days(out)[0] and "(no town near the cap: choose an overnight)" in _days(out)[0]


# --- when the router can't answer ------------------------------------------------------

def test_router_down_refuses_and_points_at_the_dated_mode(calls):
    calls.routes["osrm"] = w._Timeout("router.project-osrm.org unreachable (TimeoutError)", 0)
    with pytest.raises(w.ToolError) as caught:
        w.route_forecast({"stops": [CALGARY, CABO], "depart": "2026-10-15T10:00"})
    message = str(caught.value)
    assert message.startswith("Routing unavailable (router.project-osrm.org unreachable (TimeoutError)). ")
    assert "without depart, with a date (and hour) on each stop" in message and "estimates" in message
    assert not [u for u in calls if "api.open-meteo.com" in u]


def test_no_route_says_why(monkeypatch):
    body = (FIXTURES / "osrm_noroute.json").read_bytes()

    def refuse(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(body))

    monkeypatch.setattr(w.urllib.request, "urlopen", refuse)
    with pytest.raises(w.ToolError, match=r"Routing unavailable \(router.project-osrm.org returned HTTP 400: "
                                          r"Impossible route between points\)"):
        w.route_forecast({"stops": [CALGARY, {"latitude": 21.31, "longitude": -157.86}],
                          "depart": "2026-10-15T10:00"})


def test_an_unexpected_answer_is_logged_and_refused(calls, caplog):
    calls.routes["osrm"] = {"code": "Ok", "routes": [{"legs": []}], "waypoints": []}
    with pytest.raises(w.ToolError, match=r"Routing unavailable \(unexpected response shape"):
        w.route_forecast({"stops": [CALGARY, CABO], "depart": "2026-10-15T10:00"})
    assert any("osrm" in r.getMessage() and r.getMessage().startswith(("SHAPE", "DRIFT")) for r in caplog.records)


# --- arguments ------------------------------------------------------------------------

@pytest.mark.parametrize("args, message", [
    ({"stops": [{**CALGARY, "date": "2026-10-15"}, CABO], "depart": "2026-10-15T10:00"},
     "stop 1: date and hour are for a trip without depart"),
    ({"stops": [CALGARY], "depart": "2026-10-15T10:00"}, "at least two stops"),
    ({"stops": [CALGARY, CABO], "depart": "2026-10-15"}, "depart must be a local date and time"),
    ({"stops": [CALGARY, CABO], "depart": "2026-10-15T10:00", "start_hour": 24}, "start_hour must be"),
    ({"stops": [CALGARY, CABO], "depart": "2026-10-15T10:00", "max_drive_hours": 0.5}, "max_drive_hours must be"),
    ({"stops": [{**CALGARY, "start_hour": 6}, CABO], "depart": "2026-10-15T10:00"},
     "stop 1: start_hour belongs on an overnight stop"),
    ({"stops": [CALGARY, {**CABO, "start_hour": 6}], "depart": "2026-10-15T10:00"},
     "stop 2: start_hour belongs on an overnight stop"),
    ({"stops": [CALGARY, CABO]}, "stop 1: date is required, or pass depart"),
    ({"stops": [{**CALGARY, "date": "2026-10-15", "overnight": True}]}, "stop 1: overnight needs depart"),
    ({"stops": [{**CALGARY, "date": "2026-10-15"}], "start_hour": 6}, "start_hour needs depart"),
])
def test_mixed_or_bad_arguments_are_refused_in_one_line(calls, args, message):
    with pytest.raises(w.ToolError, match=re.escape(message)):
        w.route_forecast(args)
    assert not calls


def test_the_schema_offers_routing_and_no_longer_requires_a_date():
    schema = w.TOOLS["route_forecast"][1]["inputSchema"]
    assert {"depart", "start_hour", "max_drive_hours"} <= set(schema["properties"])
    stop = schema["properties"]["stops"]["items"]
    assert stop["required"] == ["latitude", "longitude"]
    assert {"overnight", "start_hour"} <= set(stop["properties"])
    assert schema["properties"]["start_hour"]["default"] == 10


def test_a_town_just_past_the_start_is_not_a_day(calls):
    # Leaving San Felipe the only towns before the cap are on its edge, then
    # Baja's long gap: ending the day minutes out would be no answer.
    track = w._Track([(0.0, 0.0)] * 3, [0.0, 10 * 3600.0, 20 * 3600.0], [0.0, 500.0, 1000.0], [0, 2], [0, 0], [])
    edge = w._Place("Edge", 0, 0, "MX", "BC", 1500, "America/Tijuana")
    beyond = w._Place("Beyond", 0, 0, "MX", "BCS", 9000, "America/Mazatlan")
    stops = [w._RouteStop(0, 0, "Start", False, None), w._RouteStop(0, 0, "End", False, None)]
    depart = datetime(2026, 10, 19, 10, tzinfo=timezone.utc)
    days = w._plan_days(track, stops, ["Start", "End"], [(300.0, edge), (9 * 3600.0, beyond)], depart, 10, 8)
    assert days[0].end_label == "Beyond, BCS" and days[0].end_kind == "over"


# --- review follow-ups (2026-10-14) ---------------------------------------------------

def test_an_unlabelled_stop_is_named_for_the_town_it_is_in(calls):
    stops = [{"latitude": 51.04, "longitude": -114.07}, {"latitude": 53.55, "longitude": -113.49}]
    out = _route(calls, "osrm_edmonton.json", stops, depart="2026-12-14T09:00")
    assert out.startswith("Route: Calgary, AB → Edmonton, AB, ")


def test_arriving_after_midnight_leaves_that_morning(calls):
    days = _days(_route(calls, "osrm_cabo.json", [CALGARY, CABO], depart="2026-12-14T18:00"))
    # Alberta goes to permanent UTC-6 ("CST") from November 2026 in tzdata
    # 2026e; older tzdata keeps it on MST. Found 2026-10-20 when macOS updated
    # under the suite. Either way Helena is reached after midnight, which is
    # what this test is about — so accept both rather than pin a tzdata version.
    assert re.match(r"Day 1, Mon 14 Dec: Calgary 18:00 (MST|CST) → Helena, MT 0[01]:11 MST", days[0]), days[0]
    assert days[1].startswith("Day 2, Tue 15 Dec: Helena, MT 10:00 MST → ")


def test_a_stop_named_beside_its_town_is_not_named_twice(calls, monkeypatch):
    # GeoNames has Marathon at population 0, so it never wins its hour; give
    # it Wawa's size so it would be picked beside the stop.
    towns_along = w._towns_along

    def bigger(track):
        return [(at, p._replace(population=3000) if p.name == "Marathon" else p) for at, p in towns_along(track)]

    monkeypatch.setattr(w, "_towns_along", bigger)
    out = _route(calls, "osrm_thunderbay.json", [SOO, MARATHON, THUNDER_BAY], depart="2026-12-14T09:00")
    via = " ".join(l for l in out.splitlines() if l.startswith("   via "))
    assert via.count("Marathon") == 1 and "Marathon (stop 2)" in via


def test_a_repeated_overnight_at_the_day_start_is_not_a_day():
    track = w._Track([(0.0, 0.0)] * 3, [0.0, 0.0, 5 * 3600.0], [0.0, 0.0, 400.0], [0, 1, 2], [0, 0, 0], [])
    stops = [w._RouteStop(0, 0, "A", False, None), w._RouteStop(0, 0, "A", True, None),
             w._RouteStop(0, 0, "B", False, None)]
    depart = datetime(2026, 10, 19, 10, tzinfo=timezone.utc)
    days = w._plan_days(track, stops, ["A", "A", "B"], [], depart, 10, 8)
    assert [(d.start_label, d.end_label, d.end_s) for d in days] == [("A", "B", 5 * 3600.0)]


def test_the_arrival_hour_is_read_in_the_forecasts_own_zone(calls):
    # The recording is Port Elgin's (America/Toronto). A stop timed at 19:00
    # UTC is 15:00 there, whatever zone the date and hour were first put in.
    calls.routes["weather"] = lambda url: _fixture("forecast_single.json")
    at = datetime(2026, 10, 14, 19, 0, tzinfo=timezone.utc)
    out = w._stops_weather([w._Stop(44.49, -81.38, at.date(), 19, "Port Elgin", False, at)])
    assert "   At 15:00 EDT:" in out


def test_times_with_no_town_for_a_zone_say_they_are_utc(calls, monkeypatch):
    monkeypatch.setattr(w, "_PLACES", w._PlaceIndex())
    out = _route(calls, "osrm_edmonton.json", [CALGARY, EDMONTON], depart="2026-12-14T09:00")
    assert "Times marked UTC are where no town is near enough" in out
    assert "Calgary 09:00 UTC" in _days(out)[0]


def test_an_outage_falls_back_for_the_stops_not_every_town_on_the_way(calls):
    calls.routes["weather"] = lambda url: (_ for _ in ()).throw(
        w._Transient("api.open-meteo.com returned HTTP 503: The service is overloaded", 503))
    out = _route(calls, "osrm_edmonton.json", [CALGARY, EDMONTON], depart="2026-10-13T09:00")
    fallback = out.split("this is the fallback")[1]
    assert "1. Calgary" in fallback and "2. Edmonton" in fallback and "Red Deer" not in fallback
