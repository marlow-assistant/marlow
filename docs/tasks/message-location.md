# Location on messages

**Status:** merged (2026-10-10); built on both clients:
server 1,893 / iOS 443 / web 496 tests passing; `/code-review high` (6 fixed,
1 accepted, below) and `/security-review` (no findings). **Observed on
Mike's iPhone (2026-10-10, box on the branch):** iOS permission Precise, Marlow
set to Approximate; the context line read "near <town>, <province>, Canada
(<lat>, <lon> to two decimals, ±14 km)": locality-level place, rounded coordinates, and
Core Location's reduced accuracy (14 km, above the 1 km floor). Marlow
answered "where am I?" from it. Precise, same day: four-decimal coordinates
and ±2 m, same place string (Apple's geocoder gave no neighbourhood there).
**Not yet observed:** Android Chrome's prompt and "blocked" footer.

## What Mike asked for

Raised 2026-08-12 (a Pearson transit question) and again from Quebec City on
2026-09-30, where he'd otherwise have to describe where he was. Two modes were
on the wishlist; Mike picked **the setting** on 2026-10-09: one control,
**Off / Approximate / Precise**, that attaches location to every message he
sends. The one-off "send my location on this message" button is not built.

Mike's call on the two clients (2026-10-09): **iOS adds the city name** for
Marlow's use (on-device reverse geocoding); **web sends coordinates only**.
The browser has no geocoder, and calling an outside service from it would open
an egress question for a field the model can mostly work without.

## Wire contract

`POST /v1/message` gains one optional field:

```json
"location": {
  "latitude": 46.81,
  "longitude": -71.21,
  "accuracy_m": 65,
  "place": "Old Québec, Québec, QC, Canada"
}
```

- `latitude` [-90, 90] and `longitude` [-180, 180], finite, required inside
  the object. `accuracy_m` optional, > 0. `place` optional, iOS only.
- **Absent means "no location from this device right now"** and clears what
  the server holds for that device. That one rule covers Off, a denied
  permission, and no fresh fix, with no separate "clear" call. It also means an
  older client that never sends it holds nothing, which is correct.
- A malformed object is **warn-and-drop**, the timezone field's rule: a client
  bug must not cost Mike the message he sent. Dropping also clears.
- The client sends only a fix it got within the last 15 minutes. A stale fix
  is sent as nothing.

**Approximate** is the client's job, so the precise value never leaves the
device: iOS asks Core Location for reduced accuracy (the system's own
approximate mode, a few km), and web rounds to 2 decimal places (~1 km) and
reports `accuracy_m` no better than 1,000. `place` at Approximate is the
locality and up (no street, no neighbourhood) on iOS.

## Server

`server/location.py`: the last report per device, precise in memory. It never
touches `device_tokens.json`. **Changed 2026-10-13 (`weather-mcp.md`):** it
was memory only, so a restart left the morning brief with no coordinates for
the forecast. Mike's call: a copy survives restarts in
`data_dir/last_location.json`, **rounded to 1 decimal (~11 km), with no place
string**, keyed by a hash of the device token, so it is never a home address.
Off on a device still clears its entry, restored or not.

`turn_context.build_turn_context` adds one sentence when the newest report
across devices is under **36 hours** old (12 until 2026-10-13: the 7am brief
after a quiet evening fell outside it), with its age, so a cron turn (nobody
sent it) still gets "last known, 9 h ago" for weather and the like. Older
than that it says nothing.

**Every turn, not only when it changes (Mike asked, 2026-10-09).** The
sentence is ~35 tokens, about 1K a day against a 40-120K conversation, and
rides on a context line that is sent anyway. "Only when moved" would need the
server to remember what Marlow was last told, which compaction and the nightly
fresh session silently invalidate (the `session_state.py` trap). The risk worth
watching is Marlow remarking on it, which the identity line addresses.

**Off on one device does not silence another.** The sentence states the newest
report across devices, so turning Off on the phone still leaves a web report
from the last 36 hours in place. Accepted (`/code-review high`, 2026-10-09): it
is the timezone's "most recent device wins" rule, and Mike uses the phone. A
per-user Off would need the setting on the server, which it deliberately is not.

**Approximate is enforced twice on iOS.** Core Location is asked for reduced
accuracy, and `LocationProvider.outgoing` also rounds to 2 places with a 1 km
floor, because a full-accuracy fix can still arrive across a Precise ->
Approximate switch (review finding, 2026-10-09).

**`place` reaches the prompt, so it is sanitised.** It comes from an
authenticated device, and on iOS from Apple's geocoder, so it is not hostile in
practice, but it is free text arriving on the wire. The server strips control
characters, `[` and `]` (the context block's own delimiters), collapses
whitespace and caps it at 120 characters. Coordinates are formatted from
floats, never echoed as strings.

## What this records about Mike

The context line is part of the prompt, so the location is in the CLI's
session history on the box and in whatever a compaction summarises, at the
precision Mike picked. Approximate keeps that city-level. The identity tells
Marlow not to write locations into memory unless asked.
