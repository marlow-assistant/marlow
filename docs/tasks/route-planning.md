# Route planning — real drive times for `route_forecast`

**Status: merged 2026-10-14, on-box check done the same day.**
Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

Trialled on the box before merge with four questions. Marlow routed every
time once identity said "any drive-time question", and picked better overnights
from the options line. Parked, not felt yet:
- Via times can look like they run backwards across a zone line
  ("08:00 MDT → 07:26 MST"). It's correct, and Marlow read it right twice.
- Marlow doesn't balance days (a three-day trip went 5.1 / 5.7 / 2.4 h).
- It once passed a start_hour of 8 the user never gave; the schema now says
  where these values come from.

## Why

Marlow plans road trips (van travel; Mike's 8-hour-day cap is a real
constraint) and `route_forecast` (`weather-mcp.md`) takes whatever stops and
arrival hours Marlow guesses. On 2026-10-13 its guesses were checked against
OSRM's driving times (drive only, no breaks):

| Trip | Marlow said | OSRM |
|---|---|---|
| A multi-day mountain trip | "8.5–9 h" | 11.7 h |
| ↳ day 1 ("max 8 h days") | "about 6.5 h" | 7.7 h, so over 8 with any stop |
| A highway crossing a mountain range | "about 8 h", in at 16:00 | 9.7 h, 16:45 before any stop |
| Two legs along a lake shore | 09 → 12 → 15 | 3.2 h + 2.7 h (about right) |

Single hops are within 10–20 min, but day totals drift, and badly in the
mountains. Placement fails too:
- **A stop named for an "area" was 14 km from any road**, out in a lake, so
  its weather was the lake's.
- **Two stops 40 km and a pass apart were both "14:00"**: the same instant,
  because a zone line made MST equal PDT in October.

For weather, an hour's error mostly washes out in the day/night periods. For
trip planning, the errors land exactly on what was asked.

## Decided (Mike, 2026-10-13)

- **Routing goes inside `route_forecast`, no new tool.** Marlow doesn't
  reliably chain helper tools. It had `geocode` all along and called it 3
  times while supplying coordinates for ~40 stops from its own knowledge;
  that's how the stop got into the lake. A step it can skip, it skips when
  confident. Anthropic's tool-writing guidance points the same way: fewer
  tools shaped around the task. One call answers "how long to X" *and*
  "what's the weather on the way".
- **The tool does the arithmetic and geography; Marlow keeps the judgment.**
  - The tool: road distances and times, snapping stops to the road, day
    splits at an hours cap, arrival times in each stop's own zone.
  - Marlow: which route (scenic vs fast) and where to sleep (a park or a
    campground, not whatever sits at 6:00 of driving). Mike's preferences
    live in Marlow's memory, not in a router.
  - To move an overnight, Marlow **calls again** with its chosen town as a
    stop. Same tool, so nothing to chain.
- **No traffic.** These are long rural drives, where it barely matters.
  Road conditions and closures matter more (provincial and state 511 feeds
  publish them), but that's a separate later item.
- **The tool doesn't pick overnights from amenity data.** It suggests the last
  named town before each day's cutoff. Choosing well needs campground data
  and preferences, which Marlow has or can search for.
- **Explicit `date`/`hour` per stop stays**, for one-off stops and as the
  fallback when routing is down.
- **Schema size isn't the worry it looks like.** The three weather tools'
  whole `tools/list` is 2,081 chars (~500 tokens), and the CLI defers MCP
  schemas until Marlow searches for one. Outputs are the real context cost:
  a 7-day forecast is ~3K chars. See "Output size" below.

## Decided (Mike, 2026-10-14)

- **After an overnight, leave at 10:00 local by default.** This varies a lot
  by person: some start at 05:00. It must be *very* easy for Marlow to learn
  the user's habit and use it: an argument on the call, every day line saying
  which start time it assumed and how to change it, and an identity line to
  keep the user's habit in memory once they mention it.
- **Town names come from a curated file in the repo, not a runtime
  download.** Deployed like code; no new egress. Single-digit MB per country
  is acceptable; Canada, US, Mexico and UK at minimum. Measured: CA (every
  settlement) + US/MX (pop ≥ 1,000) is 691 KB gz; UK needs every settlement
  too (some Scottish villages are pop 0 in GeoNames): 34.3K places, 490 KB
  gz. All four ≈ 1.2 MB, so one file, no per-country configuration yet.
- **Three uses to design for:**
  1. Long-range planning ("a multi-week road trip, how should I break it
     up?"): no forecast exists yet.
  2. Short-range, flexible ("a city this week, no rush, rather drive on
     poor-weather days"): which day to drive.
  3. Immediate ("driving to a nearby city today", said or implied by email or
     calendar): weather along the road at the hour you're there, alerts,
     "consider postponing".

## Shape (decided with Mike, 2026-10-14)

`route_forecast` gains a routing mode, chosen by passing `depart` (local
`YYYY-MM-DDTHH:MM` at the first stop). Stops come in order with coordinates
and an optional label and no `date`/`hour`; a stop may say `overnight: true`
(the day ends there whatever the hours) and a `start_hour`. `start_hour`
(default 10) and `max_drive_hours` (default 8) are arguments. Dated stops
together with `depart` are refused in one line, and so is a stop without
`date` when there is no `depart`.

**Routing.** One OSRM call for every stop, through `_fetch_json`, with
coordinates rounded like Open-Meteo's. The geometry plus per-segment durations
give the time at every point; towns near the road (sampled every 1 km) get
the time of their nearest approach. Near is 3 km, 6 km for 10K+ people and
10 km for 100K+, because a highway bypasses a city's centre: at 3 km a trip
between two big cities missed the mid-sized one between them. A waypoint
snapped more than 2 km is flagged. Ferries are steps whose `mode` is `ferry`.
Borders are where the nearest town's country changes and holds for 5 km: a
ferry that passes a US exclave was two false crossings without the hold.

**Days.** A day ends at a marked overnight, the destination, or the cap:
the largest town in the last hour before `max_drive_hours`, marked
"suggested" (the plain last town was a big city's suburb). With none in that
hour, the last town before the cap; with none before the cap, the first town
within 2 h after it, marked over the cap; with none there either, the road
point at the cap, for Marlow to choose. The next day starts the next calendar
day at `start_hour`, local to the overnight. Every day line says which start
and cap were assumed and that they are arguments.

**Overnight options** (Mike, 2026-10-14). The suggested overnight is only a
default; picking well is Marlow's job, not more rules'. On the box, a
6.5 h day suggested a hamlet while a real city lay 1.8 h before the cap, and
Marlow wrote "just past the city" instead of moving it. So a suggested day also
lists up to three towns from the 2 h before the cap, largest first and half an
hour apart (a big city's suburbs are one choice), with drive hours and
population. Identity says to re-call with a better option before answering.
OSRM is asked at most once a second (its demo-server policy), in the tool
itself.

**Weather, by horizon** (Marlow picks nothing):
- Departure beyond the 16-day forecast: no weather fetch; one line, the
  existing `BEYOND` wording.
- Otherwise: the start, each overnight (marked or suggested), each other
  given stop and the destination, through today's per-stop code at the
  arrival date and hour.
- A trip of one driving day that leaves within 24 hours also gets each
  via town (one per hour of driving) as a brief point: its arrival-hour
  line and its alerts only, so a warning mid-route isn't missed.

**Town names.** `server/data/places.tsv.gz` from GeoNames (CC BY 4.0,
attribution in `server/data/NOTICE`), built by `scripts/build_places.py`:
every Canadian and UK settlement, US and Mexican places of 1,000+. No
runtime download; outside these four countries the tool says it can't name
towns. The zone column gives every named town its own local time; a stop
that isn't a town takes the nearest town's zone.

**Output.** Header (total km and h, a one-line note on OSRM times, the
assumed start hour and cap), border crossings, ferries, then one line per
day with its via line (the largest town in each hour of driving, its local
time, the zone marked where it changes) and "nothing named for N h" where a
gap exceeds 2 h. Then the weather.

**Router down** (any failure, including no route): refused in one line,
pointing at the `date`/`hour` mode with Marlow's own estimates, labelled
as estimates. Never silent.

**Open and settled.** OSRM's public server is "reasonable, non-commercial use",
at most 1 request/second, no uptime promise: the same posture as Open-Meteo's
free tier, fine for one box per person, noted in `friends-alpha.md` (many more
boxes means a self-hosted OSRM or a paid router; the keyed step up would be
OpenRouteService or Mapbox, a secret on the box and so a `/security-review`).
Self-hosting OSRM for North America isn't realistic on the box (the
preprocessing needs far more RAM). A schema change reaches a running
conversation stale, so the unknown-argument refusal for a routing argument
says what changed.

## Measured (2026-10-13)

Prototype scripts were throwaway; the numbers are what matter.

1. **Naming towns.** Population-filtered GeoNames files can't be used as they
   are: **two small towns used in the trials are in none of
   `cities500/1000/5000`**, because GeoNames lists both with population 0 (as
   it does 17.8K of Canada's 21.8K populated places). Built from the
   per-country dumps instead:
   - Canada: every `PPL*` (including `PPLL`, so a remote river-crossing
     hamlet), any population. US and Mexico: population ≥ 1000, no `PPLX`.
   - **45.9K places, 691 KB gzipped** with name, lat/lon (3 dp), country,
     admin1, population and IANA zone. The zone column means every named
     town gets its own local time, not just the weather points.
   - **Load 62 ms, lookup ~50 ms** per route of ~4,100 km (28.8K polyline
     points sampled every 1 km, 0.1° grid, 3 km radius): 184 places named.
   - Mexico at ≥ 200 people is 70K places and 1.09 MB, and still leaves a
     7.3 h stretch of peninsula road with nothing named (a remote desert
     village isn't in GeoNames as populated). So the tool has to say
     "nothing named for N h" rather than pretend; the cap stays at 1000.
   - Neighbourhoods leak in. Choosing the largest place per hour of driving
     hides most of them.
   - Licence: CC BY 4.0, attribution in a NOTICE beside the file.
2. **OSRM's public server.** A 4,101 km, 50.3 h route on a long multi-day
   drive crosses two borders. One request with full
   geometry and steps is 1.0 MB and 1.5 s. OSRM doesn't mark borders; the
   country of consecutive named places does. Ferries are a step `mode`.
3. **OSRM's times against real drives.** The car profile drives at 80% of
   the posted limit (`speed_reduction = 0.8`): 110 km/h sections come back at
   exactly 88, a 90 zone at 72, a mountain parkway at 66.

   | Leg | OSRM | Google Maps, 2026-10-14 | Ratio |
   |---|---|---|---|
   | Mountain parkway leg | 417 km, 6.1 h | 4 h 34 | 1.33 |
   | Mountain highway leg | 265 km, 3.6 h | 2 h 41 | 1.33 |
   | Lake-shore highway leg | 228 km, 3.2 h | 2 h 25 | 1.34 |

   Mike's own recollections of those drives were from memory and not used.
   OSRM runs a steady third longer than Google, which assumes no stops; that
   lands near a real day on the road with short breaks. So the times go out
   unscaled, and the tool says they are OSRM's and include no stops of their
   own, so an 8 h cap is roughly 8 h on the road, not 8 h at the wheel.
   Endpoints differ slightly (Google from specific addresses, OSRM town
   centres).

**Output size, a 4,100 km trip in 8 h days:** the routing part is **~1.9K
chars** (8 days, a town per hour of driving). Weather is **~550 chars per
point** with day, night and arrival hour (measured from the fixtures, since
Open-Meteo was timing out on every request from this Mac that evening). Start,
seven overnights and the destination come to ~5K, so **~7K chars in all, ~1.8K
tokens.** Mexico has no weather fallback and no alerts source, so in an
Open-Meteo outage the Mexican stops say "unavailable".

## Constraints carried from `weather-mcp.md`

`router.project-osrm.org` joined `EGRESS_ALLOWLIST` (an egress change, so
`/security-review` as well as `/code-review high`). Routing failures and format
drift go to `weather.log` with the same verbs, coordinates cut to 0.1°. The
tests use recorded OSRM fixtures. `identity/CLAUDE.md`'s Weather section gained
one line: for a trip, give `route_forecast` the places and a departure, and let
it time the stops, because a capability the identity doesn't mention gets
routed around.
