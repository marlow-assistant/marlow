"""Town names along a route: server/data/places.tsv.gz and its lookup in
server/weather_mcp.py (docs/tasks/route-planning.md).

These run against the real data file, so a rebuild that drops the small
places the file exists for (Marathon ON, Tongue in Scotland) fails here.
"""
from __future__ import annotations

import gzip
import logging
import os
from zoneinfo import ZoneInfo

import pytest

from server import weather_mcp as w


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    # The index is cached per process; each test starts unloaded and leaves
    # nothing behind, so a test that points PLACES_PATH elsewhere cannot leak
    # its tiny index to the next.
    monkeypatch.setattr(w, "_PLACES", None)


def _find(name: str, country: str, region: str):
    index = w._places()
    hits = [p for cell in index._cells.values() for p in cell
            if p.name == name and p.country == country and p.region == region]
    return hits


def _near(places, lat: float, lon: float, km: float = 8):
    return [p for p in places if w._km(lat, lon, p.lat, p.lon) <= km]


# --- the data file -------------------------------------------------------------

def test_small_places_the_file_exists_for_are_present():
    # Marathon ON and Tongue are population 0 in GeoNames: the reason the file
    # is built from the country dumps rather than cities500.
    marathon = _near(_find("Marathon", "CA", "ON"), 48.72, -86.38)
    assert marathon and marathon[0].tz == "America/Toronto"
    tongue = _near(_find("Tongue", "GB", "Scotland"), 58.48, -4.42)
    assert tongue and tongue[0].tz == "Europe/London"


def test_larger_places_carry_region_and_zone():
    cabo = _find("Cabo San Lucas", "MX", "BCS")
    assert cabo and cabo[0].tz == "America/Mazatlan"
    calgary = _near(_find("Calgary", "CA", "AB"), 51.05, -114.08)
    assert calgary and calgary[0].tz == "America/Edmonton"
    # A US place, region = the state's own two letters.
    leavenworth = _near(_find("Leavenworth", "US", "WA"), 47.60, -120.66)
    assert leavenworth and leavenworth[0].tz == "America/Los_Angeles"


def test_file_is_small_and_every_row_is_well_formed():
    assert os.path.getsize(w.PLACES_PATH) < 2_000_000
    zones, countries = set(), set()
    with gzip.open(w.PLACES_PATH, "rt", encoding="utf-8") as fh:
        for line in fh:
            fields = line.rstrip("\n").split("\t")
            assert len(fields) == 7, line
            name, lat, lon, country, region, population, tz = fields
            assert name and -90 <= float(lat) <= 90 and -180 <= float(lon) <= 180
            assert int(population) >= 0
            zones.add(tz)
            countries.add(country)
    assert countries == {"CA", "US", "MX", "GB"}
    for tz in zones:
        ZoneInfo(tz)  # raises if the zone is unknown to this machine


def test_every_place_loads():
    # The loader skips malformed lines quietly; a rebuild that produced many
    # would otherwise shrink the file unnoticed.
    with gzip.open(w.PLACES_PATH, "rt", encoding="utf-8") as fh:
        rows = sum(1 for _ in fh)
    assert len(w._places()) == rows > 70_000


def test_label():
    assert w._Place("Wawa", 0, 0, "CA", "ON", 1, "x").label == "Wawa, ON"
    assert w._Place("Wawa", 0, 0, "CA", "", 1, "x").label == "Wawa"


# --- lookups -------------------------------------------------------------------

def test_near_finds_wawa_from_a_point_on_hwy_17():
    hits = w._places().near(47.97, -84.80, 5)
    assert hits[0][1].name == "Wawa"
    assert hits[0][0] < 5
    assert [km for km, _ in hits] == sorted(km for km, _ in hits)


CENTRES = [(48.0, -84.8), (51.05, -114.08), (47.6, -120.7), (22.9, -109.9), (58.57, -2.0),
           (51.5, -0.1), (60.7, -135.0), (49.0, -100.0), (30.0, -115.5), (45.5, -73.6)]


@pytest.mark.parametrize("radius", [3, 30, 80, 100])
def test_near_matches_a_brute_force_scan(radius):
    # Brute force over every place: a cell span that is one short anywhere
    # (either axis, any latitude) loses a place at the edge of some radius.
    index = w._places()
    everything = [p for cell in index._cells.values() for p in cell]
    for lat, lon in CENTRES:
        hits = index.near(lat, lon, radius)
        want = {p for p in everything if w._km(lat, lon, p.lat, p.lon) <= radius}
        assert {p for _, p in hits} == want, (lat, lon)
        assert [km for km, _ in hits] == sorted(km for km, _ in hits)


def test_near_80_km_has_real_content():
    # The comparison above means little on an empty answer.
    assert len(w._places().near(48.0, -84.8, 80)) > 5


def test_near_longitude_span_widens_with_latitude():
    # At 58.6N a degree of longitude is only ~58 km, so a 100 km radius reaches
    # past 1.5° of longitude: a span sized for the equator (cos(lat) dropped)
    # stops ~0.9° short and misses the places out east and west.
    index = w._places()
    lat, lon = 58.57, -2.0
    got = {(p.name, p.lat, p.lon) for _, p in index.near(lat, lon, 100)}
    assert any(abs(plon - lon) > 0.9 for _, _, plon in got)


def test_nearest():
    index = w._places()
    assert index.nearest(47.97, -84.80, 10).name == "Wawa"
    assert index.nearest(40.0, -40.0, 100) is None  # mid-Atlantic


# --- the polyline decoder ------------------------------------------------------

def _flat(points):
    # pytest 9 refuses approx on nested sequences, so compare coordinates flat.
    return [c for p in points for c in p]


def test_decode_polyline_google_example():
    points = w._decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@", precision=5)
    assert _flat(points) == pytest.approx(_flat([(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]))


def test_decode_polyline_defaults_to_precision_6():
    assert _flat(w._decode_polyline("_p~iF~ps|U")) == pytest.approx([3.85, -12.02])


@pytest.mark.parametrize("encoded", ["_p~iF~ps|U_ulL", "_p~iF~ps|U_", "_p~iF~ps|"])
def test_decode_polyline_truncated_raises_value_error(encoded):
    with pytest.raises(ValueError):
        w._decode_polyline(encoded, precision=5)


def test_decode_polyline_empty():
    assert w._decode_polyline("") == []


# --- a file that is missing or damaged -----------------------------------------

def test_missing_file_gives_an_empty_index_and_one_warning(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(w, "PLACES_PATH", str(tmp_path / "nope.tsv.gz"))
    with caplog.at_level(logging.WARNING, logger="marlow.weather"):
        assert len(w._places()) == 0
        assert len(w._places()) == 0  # cached: no second warning
    assert [r.getMessage().split(":")[0] for r in caplog.records] == ["PLACES unavailable"]


def test_malformed_line_is_counted_and_the_good_one_loads(monkeypatch, tmp_path, caplog):
    path = tmp_path / "places.tsv.gz"
    path.write_bytes(gzip.compress(
        "not a row\nWawa\t47.99\t-84.77\tCA\tON\t2905\tAmerica/Toronto\nBad\tx\t1\tCA\tON\t1\tUTC\n".encode()))
    monkeypatch.setattr(w, "PLACES_PATH", str(path))
    with caplog.at_level(logging.WARNING, logger="marlow.weather"):
        index = w._places()
    assert len(index) == 1 and index.nearest(47.99, -84.77, 5).label == "Wawa, ON"
    drift = [r.getMessage() for r in caplog.records]
    assert len(drift) == 1 and drift[0].startswith("DRIFT places: 2 malformed")


def test_corrupt_gzip_gives_an_empty_index(monkeypatch, tmp_path, caplog):
    path = tmp_path / "places.tsv.gz"
    path.write_bytes(gzip.compress(b"x" * 5000)[:30])
    monkeypatch.setattr(w, "PLACES_PATH", str(path))
    with caplog.at_level(logging.WARNING, logger="marlow.weather"):
        assert len(w._places()) == 0
    assert any("PLACES unavailable" in r.getMessage() for r in caplog.records)
