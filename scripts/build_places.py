#!/usr/bin/env python3
"""Build server/data/places.tsv.gz, the town-name file route_forecast reads.

    python3 scripts/build_places.py <geonames_dir> <out.tsv.gz>

<geonames_dir> holds GeoNames' per-country dumps CA.txt, US.txt, MX.txt and
GB.txt, plus admin1CodesASCII.txt (all from https://download.geonames.org/
export/dump/; the dumps are zipped there). Hand-run, stdlib only; commit the
output. Run `python3 -I` if the directory came from the network.

Why not the stock cities500/1000/5000 files: GeoNames lists most small
Canadian and UK places with population 0, so Marathon ON, Plain WA, and
Tongue, Durness and Glencoe in Scotland are in none of them (measured
2026-10-13, docs/tasks/route-planning.md "Measured"). A route has to name the
towns on the road, small ones included, so this builds from the country dumps:

- CA and GB: every settlement code at any population, plus any other P code
  with population >= 1000.
- US and MX: population >= 1000. (Mexico at >= 200 is 70K places and still
  leaves Baja with gaps; the tool says "nothing named for N h" instead.)
- Never PPLX (section of a place), PPLH (historical), PPLQ (abandoned),
  PPLW (destroyed).

Output: gzip TSV, no header, sorted by country then name so a rebuild diffs
stably. Columns: name, lat, lon, country, region, population, IANA zone.
`region` is what a traveller writes after the town (ON, WA, BCS, Scotland).

Licence: GeoNames data is CC BY 4.0, attribution in server/data/NOTICE. This
file is a modified (filtered, reduced) copy.
"""
from __future__ import annotations

import gzip
import os
import sys
import unicodedata

SETTLEMENTS = {"PPL", "PPLA", "PPLA2", "PPLA3", "PPLA4", "PPLC", "PPLL"}
NEVER = {"PPLX", "PPLH", "PPLQ", "PPLW"}
ALL_SETTLEMENTS = {"CA", "GB"}
COUNTRIES = ("CA", "US", "MX", "GB")
MIN_POPULATION = 1000
MAX_GZ_BYTES = 2_000_000

CA_ABBREV = {
    "Alberta": "AB", "British Columbia": "BC", "Manitoba": "MB",
    "New Brunswick": "NB", "Newfoundland and Labrador": "NL",
    "Nova Scotia": "NS", "Northwest Territories": "NT", "Nunavut": "NU",
    "Ontario": "ON", "Prince Edward Island": "PE", "Quebec": "QC",
    "Saskatchewan": "SK", "Yukon": "YT",
}
# Keyed by admin1CodesASCII's ASCII name column ("Mexico" is the state,
# "Mexico City" the capital).
MX_ABBREV = {
    "Aguascalientes": "Ags.", "Baja California": "BC",
    "Baja California Sur": "BCS", "Campeche": "Camp.", "Chiapas": "Chis.",
    "Chihuahua": "Chih.", "Coahuila": "Coah.", "Colima": "Col.",
    "Mexico City": "CDMX", "Durango": "Dgo.", "Guanajuato": "Gto.",
    "Guerrero": "Gro.", "Hidalgo": "Hgo.", "Jalisco": "Jal.",
    "Mexico": "Méx.", "Michoacan": "Mich.", "Morelos": "Mor.",
    "Nayarit": "Nay.", "Nuevo Leon": "NL", "Oaxaca": "Oax.",
    "Puebla": "Pue.", "Queretaro": "Qro.", "Quintana Roo": "Q. Roo",
    "San Luis Potosi": "SLP", "Sinaloa": "Sin.", "Sonora": "Son.",
    "Tabasco": "Tab.", "Tamaulipas": "Tamps.", "Tlaxcala": "Tlax.",
    "Veracruz": "Ver.", "Yucatan": "Yuc.", "Zacatecas": "Zac.",
}
GB_REGIONS = {"ENG": "England", "SCT": "Scotland", "WLS": "Wales", "NIR": "Northern Ireland"}


def clean(text: str) -> str:
    """Tabs, newlines and other control characters would break the TSV."""
    return "".join(" " if c in "\t\r\n" else c for c in text
                   if c in "\t\r\n" or unicodedata.category(c)[0] != "C").strip()


def admin1_names(path: str) -> dict[str, str]:
    names = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            f = line.rstrip("\n").split("\t")
            names[f[0]] = f[2]  # ASCII name
    return names


def region(country: str, admin1: str, names: dict[str, str]) -> str:
    """"" for a place GeoNames gave no admin1 (Brookside NL, a few US census
    areas); the lookup then prints just the name. An admin1 that is given but
    unknown is a change in GeoNames' data, so it stops the build."""
    if not admin1 or country == "US":
        return admin1
    if country == "GB":
        if admin1 not in GB_REGIONS:
            sys.exit(f"unknown GB admin1 {admin1!r}")
        return GB_REGIONS[admin1]
    table = CA_ABBREV if country == "CA" else MX_ABBREV
    name = names.get(f"{country}.{admin1}")
    if name not in table:
        sys.exit(f"no abbreviation for {country}.{admin1} ({name!r})")
    return table[name]


def wanted(country: str, code: str, population: int) -> bool:
    if code in NEVER:
        return False
    if country in ALL_SETTLEMENTS and code in SETTLEMENTS:
        return True
    return population >= MIN_POPULATION


def build(gdir: str) -> list[tuple]:
    names = admin1_names(os.path.join(gdir, "admin1CodesASCII.txt"))
    rows = []
    for country in COUNTRIES:
        with open(os.path.join(gdir, f"{country}.txt"), encoding="utf-8") as fh:
            for line in fh:
                f = line.rstrip("\n").split("\t")
                if f[6] != "P":
                    continue
                population = int(f[14] or 0)
                if not wanted(country, f[7], population):
                    continue
                if country == "GB" and not f[10]:
                    continue  # the Cyprus bases (Akrotiri, Dhekelia): no admin1
                rows.append((country, clean(f[1]), f"{float(f[4]):.3f}", f"{float(f[5]):.3f}",
                             region(country, f[10], names), population, f[17]))
    rows.sort(key=lambda r: (r[0], r[1], r[2], r[3]))
    return rows


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    rows = build(sys.argv[1])
    text = "".join(f"{n}\t{la}\t{lo}\t{c}\t{rg}\t{p}\t{tz}\n" for c, n, la, lo, rg, p, tz in rows)
    with open(sys.argv[2], "wb") as fh:
        # mtime=0: the same rows give the same bytes, so a rebuild diffs clean.
        with gzip.GzipFile(fileobj=fh, mode="wb", compresslevel=9, mtime=0, filename="") as gz:
            gz.write(text.encode("utf-8"))
    size = os.path.getsize(sys.argv[2])
    for c in COUNTRIES:
        print(c, sum(1 for r in rows if r[0] == c))
    print(f"total {len(rows)} rows, {size} bytes gz")
    if size > MAX_GZ_BYTES:
        sys.exit(f"over {MAX_GZ_BYTES} bytes: tighten the filter")


if __name__ == "__main__":
    main()
