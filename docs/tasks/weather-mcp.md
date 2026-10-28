# Weather MCP server — dated forecasts, with national fallbacks

**Status: merged 2026-10-13, live on Mike's box. Still open:
`weather.log` after a few days (`RETRY ok` against `FALLBACK used`); then,
with Mike's OK, the wishlist entry and the travel.md line (see "Done when").
Routing is its own task: `route-planning.md`, merged 2026-10-14.**
Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

Server-only: **neither client needs a UI change.** iOS and web/Android both
reach this through Marlow's replies and already send the coordinates it uses
(`message-location.md`), so "done" here needs no client half.

## Why

On 2026-10-11, asked for a week of weather along a long highway, WebSearch
returned a July forecast for one town and 26°C for another in October. Search
results are pages of unknown date, and the model cannot tell a stale page from
a current one. **Stale or mixed-date data is the bug**, so the fix is a source
that is current by construction and an output where every value carries its
date. The wishlist entry is "Reliable weather retrieval (2026-10-11)" in the
box's `memory/marlow-features.md`.

## Decided (Mike, 2026-10-13; don't re-open)

- **Open-Meteo** as the primary source: free, no key, covers North America,
  and carries Environment Canada's GEM models.
- **Our own small server**, not a third-party Open-Meteo MCP (17 tools, an npm
  supply chain, generic output up to 25K characters). It served only as a
  reference.
- **Python, stdlib only, in this repo** (`server/weather_mcp.py`). Stdio MCP
  is done by hand: newline-delimited JSON-RPC, `initialize` / `tools/list` /
  `tools/call` / `ping`. No new dependency.
- **No `--strict-mcp-config`, no `~/.claude.json`.**
- **Route = stops supplied by Marlow.** A router would be another host and
  more code, and Marlow already plans the trip. Superseded 2026-10-14:
  Marlow's day totals ran up to 30% low, so `depart` now routes the trip
  through OSRM (`route-planning.md`); the dated mode stays as the fallback.
- **The server falls back by itself, not the model.** Open-Meteo was down
  for 10–15 minutes (503 "overloaded", timeouts) on the morning this was
  written. Mike's call: an identity rule telling Marlow to go find a national
  weather page is one more thing for the LLM to coordinate, and a model left to
  improvise reaches for WebSearch. So after one retry the server asks a
  national service itself: **Environment Canada** for Canada, **MET Norway**
  for the UK (the Met Office needs a key that Marlow could read from inside the
  sandbox; MET Norway is keyless and ECMWF-based), and the **US NWS** for the
  US. Anywhere else the answer is "unavailable".
- **Location survives a restart, rounded** (below), and the context line's
  window grows from 12 h to 36 h.
- **Alerts on every answer, for Canada and the US** (Mike, 2026-10-13, after
  Marlow's first-use feedback below; this reverses the morning's "not now").

## Location: precise to Marlow, rounded everywhere else

Mike asked whether the app's location gets reduced to a city before Marlow
sees it. **It doesn't.** The context line carries the coordinates next to the
place name: at Precise, four decimals and "±2 m". So `forecast` takes them
straight from that line, and `geocode` is for **other** places. The identity
says so, since the model will otherwise geocode the place name out of habit.

Two roundings protect that precision (Mike, 2026-10-13):

- **To Open-Meteo and the fallbacks: 2 decimals (~1 km)**, enforced in the
  server so the model can't send more. That's finer than any model grid
  (HRDPS is 2.5 km), so the forecast doesn't change. Coarser was refused: on a
  lake shore, 10 km decides whether you get the town's forecast or open
  water's.
- **On disk: 1 decimal (~11 km), no place string.** `server/location.py`
  keeps a copy in `data_dir/last_location.json`, keyed by a hash of the
  device token, so a restart doesn't leave the morning brief without a
  position. A restored entry reports ±10 km. `data_dir` is hidden from the
  agent by bwrap. See `message-location.md`.

With no location line at all (nothing in 36 h), the identity falls back to the
home town Marlow knows from memory.

## Tools

Three read-only tools: `geocode`, `forecast` (day and night periods for the
first week, then one row per day to 16 days; `hours` adds hourly rows) and
`route_forecast` (up to 12 stops, one request). **Every line names its date**
in the location's local time, labelled with the zone abbreviation in force at
that hour, so an hourly row past a DST change says EST, not EDT. Units are
metric. A date outside the forecast is refused, pointing at typical conditions
for the month, rather than guessed.

**Model: GEM first, then `best_match`, one model per row.** Measured
2026-10-13: Open-Meteo's `best_match` over Ontario is NOAA's GFS (identical
numbers to `gfs_seamless`), not GEM, and GEM was the reason for choosing
Open-Meteo. GEM ends after about 9 days. So each request asks for
`gem_seamless,best_match`, and each row takes all its values from GEM where
GEM has that day, otherwise from best_match. Every row is tagged `[GEM]` or
`[best match]`.

**Fallback detail is thinner.** Environment Canada gives its own text, by
day/night period, for about 6 days, with any active warnings. NWS gives 7
days of day/night periods in °C. MET Norway's series is summarised into UK
day and night periods, for about 10 days. None of them gives hourly rows. The
answer always opens with "Open-Meteo unavailable (…)" and names the source and
when it was issued, so the transcript records every outage, and `weather.log`
does too (see "Logging"). A fallback that fails too says to tell the user the
forecast is unavailable and not to search.

**Picking a fallback:** Environment Canada if it has a city page within 40 km;
otherwise MET Norway if the point is inside a box around Great Britain and
Ireland (northern France excluded); otherwise NWS, whose 404 means "not the
US". A US point near the border can get a Canadian city page, which is
acceptable for a fallback. Two checks guard the fallback requests: the
Environment Canada id is shape-checked before it goes into a URL path, and only
NWS's own `api.weather.gov/gridpoints/` link is followed.

## After first use (2026-10-13)

Asked about the tool after the first live use, Marlow named its own gaps. Mike
picked four of them, plus eager loading:

1. **Precipitation is labelled:** "7.7 mm total, 100% chance" for days, "0.0 mm
   this hour, 95% chance" for hours. A 95% chance at 0.0 mm is real GEM output
   (amount and chance come from different calculations), but unlabelled it
   read as a bug.
2. **Alerts:** every `forecast` answer and every route stop carries one line:
   what's in effect, "none in effect (source)", or "could not check (reason)".
   A failure never reads as "none".
   - **Canada uses Environment Canada's alert polygons** (`weather-alerts`),
     tested against the exact point. The first version read the warnings on
     the *nearest city page*, and Marlow caught what that costs: a stop got
     the page of a town 44 km away across the water, a different warning area.
     Ended, cancelled and expired alerts are dropped.
   - **US: NWS `alerts/active?point=`**, asked *before* Canada is assumed. A US
     city within 40 km of a Canadian city page was told "none (Environment
     Canada)" in a live check. NWS `/points` 404s on Canadian points, so it
     settles which side a point is on.
   - With no alert at a Canadian point, "none in effect" needs an Environment
     Canada city page within 150 km. A remote shoreline point ~50 km from any
     page got a US all-clear in an earlier version. The UK says "not checked".
   - Alerts make one quick attempt (4 s, no retry), and a route checks its
     distinct points concurrently within 20 s.
3. **A window per route stop** (`from_hour`/`to_hour`): built, then replaced
   the same day by day/night periods (below), before anything used it. Mike
   found "window" non-obvious, and an overnight window was a camping
   parameter in a schema everyone pays for.
4. **Snowfall** in cm on daily and hourly rows, when it's above zero. The
   daily condition reads "worst: heavy drizzle", because Open-Meteo's daily
   code is the day's most severe. Unlabelled, it sat beside a 0.4 mm arrival
   hour and read as a contradiction (Marlow's second round).
   Visibility and freezing level were left out: GEM over Ontario has neither,
   so adding them would mix models within a row.

Declined:
- **Route by start, end and drive time.** It needs a routing service: another
  host with no uptime promise.
- **Place names in stops.** A name can match several places, so the tool
  would have to guess; `geocode` is one call.

**Loaded eagerly (`alwaysLoad`).** Mike wanted weather in front of the agent
rather than behind a ToolSearch round trip. The 2026-08-23 notes
(`marlow-settings-permissions-policy.md`) found no switch for MCP tools at
2.1.221. The CLI has one now: a per-server `"alwaysLoad": true` in the
`mcpServers` entry, described in its own schema as "all tools from this server
are always included in the prompt and never deferred". It's present at 2.1.284
on the box. Its side effect is that startup waits up to 5 s for the server to
connect; this server makes no network call at startup, so that costs nothing.
The price is three tool schemas on every turn, cron included. **To verify on
the box:** a weather question answered with no `ToolSearch` call before the
weather tool.

## The shape (Mike, 2026-10-13)

Settled after both rounds of feedback, across three uses: daily living ("jacket
today?", "frost tonight?", the morning brief), vacation (a named place on
given dates), and road trips and camping (each stop when you are there, and
the night at camp).

- **Day 06:00–18:00 and night 18:00–06:00 next morning, for 7 days**, then
  one row per day to 16. Environment Canada and the NWS publish periods the
  same way, so the fallbacks read alike. A night carries the real overnight
  low, which answers "tonight" and the camp question with no camping
  parameter. Each period summarises its hours: low–high, up to three
  conditions (the three most severe, in the order they come), total
  precipitation and peak chance, snow, the windiest hour's wind, and the
  worst gust. A day row carries sunrise and sunset.
- **The clock trims today.** At 08:30 today's day runs 08:00–18:00; after dark
  only tonight is left; before dawn the first row is the rest of last night,
  dated yesterday. A period the data ends partway through says where.
- **Hour by hour only for today and tomorrow** (`hours`): about 48 hours is
  how far the high-resolution GEM (HRDPS) runs. Past that, an hourly figure is
  false precision the model would repeat. An enum rather than a date, so the
  cap is in the schema and Marlow does no date arithmetic for "this
  afternoon".
- **A route stop's arrival `hour` reaches 72 hours from now**, by the clock.
  It's one line, not a day of them, and 72 h is inside the regional GEM's ~84.
  Originally it was "today and tomorrow" by calendar date, which in a Tuesday
  evening plan dropped Thursday's 10:00 arrival at a stop 41 h away (Mike,
  2026-10-13).
- **Past 16 days**, the refusal points at typical conditions for the month.
  Normals don't go stale, so a search for them is fine; there's no climate
  tool.

Cost: a 7-day answer is ~2.8K characters against ~1.1K for daily rows, on the
morning brief and wherever else it's used. Every request now asks for hourly
data; the hours stay server-side.

## Logging

`~/.marlow/logs/weather.log` (`invoke.py` passes `--log`). Every source is
someone else's service with no promise to us, so the log exists to catch a
format change (Mike, 2026-10-13). **Problems only**, so a quiet file means
quiet operation, and `cat` is the whole review:

- `FETCH`: any failed request, with host and path (no query; coordinates cut
  to 0.1°, the precision the disk is allowed). NWS's 404 outside the US is an
  answer and isn't logged.
- `FALLBACK used`: an Open-Meteo outage, with the source that answered.
- `RETRY ok`: a quick failure (503, 429, refused connection) that the one
  retry fixed. The retry policy (Mike, 2026-10-13): a 5 s timeout. A timeout
  is not retried and goes straight to the fallback. Anything else waits 1 s,
  or a `Retry-After` of up to 3 s; a longer one goes to the fallback. A few
  weeks of these lines against `FALLBACK used` say whether the retry earns
  its place. Background: three Open-Meteo outages on 2026-10-13. The free
  tier promises no uptime; the paid API (dedicated servers, 99.9% target)
  is the answer if outages turn out to be daily.
- `SHAPE`: a response that broke the parsing (KeyError and the like), with
  the traceback.
- `DRIFT`: a response that parsed but yielded nothing usable. This is the
  dangerous kind, because it fails *quiet*. A renamed `alert_name_en` used to
  skip every alert and print "none in effect". Now the alert is reported as
  "unnamed alert (feed format changed)", and the log lists the keys it did
  find. The same goes for a non-polygon alert area, an unknown `status_en`
  (still counted as in effect), an alert feed with no `features` ("could not
  check", never "none"), a city page whose periods no longer parse, and an
  Open-Meteo answer with no hourly block (daily rows instead).
- `INTERNAL`: a bug in this server, with the traceback.

It's a separate file with plain appends, and it doesn't rotate, for
`get_deny_logger`'s reasons. Nothing reads it automatically yet:
`failure-alert-channel.md` is the open item that would.

## Second round of Marlow's feedback (2026-10-13)

A multi-day road trip across a province. Its points, and what we did:

- **"The overnight window didn't work."** True, and it was our bug. Marlow
  sent the retired `from_hour`/`to_hour` one minute after the redeploy,
  because the CLI had shown it the old schema. A deferred MCP tool's schema
  is recorded in the session when it is first loaded (`deferred_tools_record`
  12:58Z, `prompt_snapshot` 20:58Z, both pre-deploy) and replayed on
  `--resume`. Expect any schema change to reach the running conversation
  stale until it starts afresh. The server ignored unknown keys, so the stop
  came back plain with no word.
  Unknown arguments are now refused, and the retired names (`hourly_date`
  too) say what replaced them. The night period was the answer all along.
- **"The fallback gave no warning when it changed the answer."** Not what
  happened. `weather.log` was empty, so there was no fallback. The run half an
  hour earlier was the pre-update server, which had daily rows only. Every row
  already names its source.
- **Conditions in a 12-hour block don't say when; can't say when the
  rain peaks.** Built (Mike's call). Each condition carries its runs
  ("drizzle 01:00–06:00"; "on and off 06:00–12:00" past two runs), and the
  rain total names its heaviest hour. Building it showed that Open-Meteo's
  hourly `weather_code` and precipitation probability are for the hour
  *ending* at T, like the rain: code 80 sits exactly on the rainy hours. So
  periods take conditions from the same hours as rain, and the hour-by-hour
  lines now label them "08:00–09:00" where they used to say "this hour". Before
  that, Marlow's "9am drizzle" was really 08–09.
- **"Up to 81%" is a maximum.** The row says "up to". No change.
- **No hour-by-hour after tomorrow; no drive times.** Both deliberate: the
  ~48 h cap is Mike's call (HRDPS range). Drive times came later, with
  `depart` (`route-planning.md`).
- **24-hour times.** Marlow converts them using the user's saved preference.
  Wording belongs on the reading side, not here.

## Third round: a long route, in an outage (2026-10-13)

Open-Meteo timed out on both attempts (its third outage that day), so the
whole route fell back to Environment Canada. Marlow's points, checked:

- **"A one-line 'source degraded' warning."** It was already the first line,
  and Marlow quoted it. No change.
- **"One line is wrong; 0 km looks buggy."** No. ECCC pins one town's page on
  a neighbouring town's coordinates, and both pages are one regional forecast
  with identical text.
- **"Two of seven stops returned nothing."** True, and the real bug. A town
  was 80 km from its own pin, and a remote park ~60–75 km from any page.
  Fixed: within 40 km a page is local. The NWS is asked next, so a US point
  near the border keeps its own forecast. Then comes a page out to 100 km,
  labelled "a regional guide for this point, not a local forecast". The region
  is named in every case.
- **"10am at a stop was a morning block."** The parked "hourly in the
  fallbacks" trigger, fired. ECCC city pages carry 24 hourly forecasts; they
  now answer `hours` and a route stop's `hour`, and say where they end. MET
  Norway and NWS hourly data are still unwired (no outage has needed them).
- **Elevation, mixed issue times, road conditions.** Limits of the source, or
  not weather.

Known, not fixed:
- A far page's zone comes from its province, so a stop in one time zone
  answered by a page in the next reads "13:00 MDT". The zone is printed.
- A US point within 40 km of a Canadian page gets ECCC, not the NWS (22 km in
  one case). That was already the case before the radius change.

## How `claude -p` gets it

`invoke.py` adds **`--mcp-config=<inline JSON>`** on every call
(`weather_mcp_config()`): `sys.executable -I <server/weather_mcp.py>`.

- **Inline JSON, not a file.** This changes the brief, which said "a
  harness-owned file", and keeps its intent. There's no config file on disk, so
  there's nothing for `AGENT_CONFIG_PATHS` to protect, and the paths are right
  on any box.
- **Single token.** The option is variadic like `--tools`, and the two-token
  form eats the prompt. It sits ahead of `--`.
- **`-I`**: the CLI spawns the server with cwd = the agent-writable working
  dir. Isolated mode keeps that directory, the script's own directory,
  `PYTHON*` env and the user site-packages off `sys.path`. That's why the
  module is a single standalone file.
- It runs inside the CLI's bwrap namespace: secrets are hidden, and the
  network is shared.
- **The script and interpreter are not writable by the agent**, prompted or
  not. `deploy/marlow-server.service` sets `ProtectHome=read-only`, with
  `ReadWritePaths` limited to `~/.marlow`, `~/.claude`, `~/marlow-identity` and
  the cron spool. `~/marlow-server` and `~/.venvs` are therefore read-only
  mounts that bwrap inherits (`/security-review`, 2026-10-13; this corrects an
  earlier line here that called the server code prompt-writable).

### Probe (2026-10-13, passed)

`scripts/probe-weather-mcp.sh` ran on the box as `marlow` at CLI 2.1.284. It
used the deployed `_confinement_prefix` and the same inline single-token form.
The init event listed `mcp__weatherprobe__net_api_200_geo_200`, the probe
server's tool with both Open-Meteo hosts reached from inside bwrap. Gmail (30
tools) and Calendar (9) were listed alongside it, with `mcp_servers` showing
the probe server as `"source": "dynamic"` beside the `claudeai` connectors. No
tool ran.

## Permissions, egress, identity

The tools are read-only and must work in cron, so they're allowed in **both**
`deploy/marlow-settings.json` and `boundaries.VETTED_PATHLESS_TOOLS` (the
hook's decision wins over settings); a test keeps the lists in line.
`boundaries.EGRESS_ALLOWLIST` gained the Open-Meteo, Environment Canada, MET
Norway and NWS hosts, and a test fails if the server names one that isn't
listed. **The allowlist isn't enforced yet** (`deploy/README.md` §9, Known gap
2); when it is, these hosts may rotate IPs the way `api.anthropic.com`'s do.
`identity/CLAUDE.md` gained one line (Mike, 2026-10-13: the tool descriptions
carry the rest): use the `weather` tools for any forecast, never search, and
pass the coordinates from the context line. It stays in the identity because
the habit being replaced is WebSearch, and an unmentioned feature gets routed
around. `identity/cron/email-flash.md` now says "weather tools, never search".
`identity/cron/morning.md` gained a Weather section, **but that file is
a starter that becomes the user's**, so existing boxes get it from migration
`0004-morning-weather`, which skips any brief that already mentions weather;
that covers the Android user's box. Mike's was edited by hand on 2026-10-13, with his OK.

## Done when (what remains)

- On the box: after a few days, `weather.log` holds nothing unexpected.
- **Then** (Marlow's files, so ask Mike first): remove the wishlist entry
  from `marlow-features.md`, and the line in `memory/travel.md` that calls
  weather lookups unreliable.

## Parked

- **`geocode` has no fallback.** Geocoding stayed up during the 2026-10-13
  outage, and for a well-known town Marlow can usually pass coordinates
  itself. It's released by a geocode failure that cost an answer.
- **MET Norway and NWS hourly detail** (ECCC's is wired): released by a missed
  arrival-time forecast during an outage in the UK or the US.
- **Other countries' fallbacks**: released by a trip there.
- **A router for "along a highway"** with no stops given: released if Marlow's
  own stop choices prove bad in practice.
