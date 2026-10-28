# Task — the transcript is a single growing file, rewritten whole on every event

**Status:** rendering half built 2026-09-25, **confirmed on Mike's
phone the same day**: a cold launch now reads as instant. Trigger: Mike reported a half-second-to-a-second
blank screen on every cold launch, the growth the eager-rows note below
predicted. `TranscriptWindow` renders rows from a date floor, about 48 hours
back with a minimum of 30 rows, set once at launch. "Show earlier messages"
steps back one more window per tap. `ingestUnsolicited` lowers the floor for
a turn that arrives older than it. The file stays whole. **The persistence and
durability halves below are untouched.** They still have no form decided, and
the options section is still not a recommendation. A size cap on the window
is designed but parked. See "Rendering window — device measurements" below
for the numbers and what would release it.

## What's actually true today

`ChatStore` (`ios/Marlow/Marlow/Models/ChatStore.swift`) keeps the whole
conversation as one `[ChatMessage]` array in memory, and
`persist()`/`load()` (same file) read/write it as one JSON file at
`makePersistURL()` — `Application Support/chat-transcript.json`. `persist()`
re-encodes the *entire* array and rewrites the whole file on every status
transition: every poll tick that lands `.running` → `.done`, every ingested
cron turn, every retry. That's O(n) work per event, where n is the
transcript's whole lifetime, not just what changed — it gets slower forever,
and invisibly, since a few hundred messages costs nothing and the failure
mode is a slow creep rather than a cliff.

`ChatMessage` itself (`ios/Marlow/Marlow/Models/ChatMessage.swift`) is the
unit being persisted — its shape (and any format migration) is the other
thing a fix would touch.

**Rendering used to be out of scope here. As of 2026-09-06 it is in scope,
because `LazyVStack` is gone.** The paragraph that used to sit here said
rendering was not part of this problem, since the transcript only built views
for rows near the visible area regardless of length. That is no longer true.

`LazyVStack` was the cause of a cold-launch bug: on device (iPhone 14 Pro, 205
messages) the app opened to a black screen for several seconds and painted the
whole transcript the instant Mike scrolled. Instrumentation counting realized
rows found LazyVStack had built **zero** of them, for eight seconds — it
measured one screenful, concluded from its own truncated height that there was
nothing more to scroll to, and never asked again. Removing the wrapping
`GeometryReader` moved that from 0 rows to 5; only eager rows fixed it. The
simulator never reproduced any of it, at 120 or at 2500 messages. Full
reasoning is in the `ChatView` type comment.

So `ChatView` built **every** message at launch, and that cost grew with the
transcript forever, the same unbounded-growth shape as `persist()` below, on a
different axis. That was the cold-launch blank Mike reported on 2026-09-25.
Since the windowing change it builds only the window (next section).

**Mike's decision rule for a fix (2026-09-06), which is what makes this
actionable rather than a worry:** roughly **48 hours of messages** need to
render eagerly for scrolling to feel immediate. Older than that, a visible
pause of about half a second while more rows load is explicitly acceptable. So
the shape of the fix is a rendered *window* over an intact file — render the
recent window eagerly, load older rows on demand — and NOT a return to
`LazyVStack`, which fails on device in the way described above no matter how
few rows it holds. Anyone picking this up must re-test on hardware; the
simulator will report success either way.

Note this couples to the durability half below in a useful direction: a
rendered window is not a pruned file, so it costs nothing in history.

## Rendering window — device measurements, 2026-09-25

Measured on Mike's iPhone 14 Pro with the Debug build, launched by a tap after
a force quit. Instrumentation was uncommitted: a one-shot `CADisplayLink`
whose first tick after a state change marks the end of SwiftUI's
build/layout/commit, so each figure is an upper bound that includes up to one
frame of vsync wait. With the Xcode debugger attached, launch was visibly
slower; those numbers are not representative. Transcript: 575 rows, 338.6k
characters.

| Event | Rows shown | Chars shown | Time |
|---|---|---|---|
| Launch (30-row window) | 30 | 17.8k | 277ms since `ChatView.init`, 381ms since process start |
| Reveal 1 | +54 → 84 | +71.7k → 89.6k | 196ms |
| Reveal 2 | +77 → 161 | +43.1k → 132.7k | 180ms |

Solving the two reveals: about **1.4ms per row plus 1.7ms per 1,000
characters**. That is two points for two unknowns, so there is no noise
estimate, and it ignores re-checking the rows already on screen, which inflates
the per-row term somewhat. The direction is solid; the constants are rough.
What follows from it:

- **Neither rows nor characters alone predict the cost.** A typical ~800-char
  row is half row overhead, half text. A row cap misses long replies; a
  character cap misses chatty stretches.
- **Launch is mostly fixed cost.** The 30-row window costs about 70ms, and the
  other ~205ms is decoding the whole file, the rest of the screen, and the
  unoptimized build. Pricing the full transcript the same way gives about
  900ms of rows, which matches the blank Mike saw before the windowing change.
- **The launch animation is what hides the rest, and the margin is thin.**
  381ms since process start is inside iOS's open animation (roughly
  400–500ms, not measured here), which is why it reads as instant, but only by
  about 100ms. A busy 48 hours of long replies could spend that.

**Parked: a weighted size cap on the window.** Keep the 48-hour rule, but stop
adding rows once `chars + 800 × rows` passes a budget of roughly 100–150ms at
the rates above, with the 30-row minimum made best-effort under it. It belongs
in `TranscriptWindow.floor`, with tests. **Build it when** a cold-launch blank
is seen again, or a re-instrumented launch goes above about 450ms since process
start. Not before: launch reads as instant today, and a busy-enough 48 hours
has not been observed. Also not worth doing: raising the 30-row minimum, since
the margin can't pay for it.

## The constraint that makes this not a copy-paste of Messages/Discord

Those apps window their *client* while their *server* holds the permanent,
complete history — pruning or paginating the client loses nothing there,
because the source of truth is elsewhere.

That's not true here. `server/queue.py`'s `WorkQueue._turns` is an in-memory
dict with **no retention policy** — a server restart clears it, and
`ChatMessage.swift`'s own doc comment already says so: "the server's `_turns`
dict is in-memory with no retention policy... the app must not assume
history survives server-side." **The iOS client's local JSON file is the
only durable copy of the conversation that exists anywhere.** Any future fix
that prunes or expires local history is data loss, full stop, unless
server-side retention (`nightly-consolidation.md`) lands first — that task
and this one are coupled even though they live in different parts of the
codebase.

**Observed, not theoretical (2026-08-30).** Deleting and reinstalling the app
to chase a first-launch bug took the entire transcript with it — Mike lost
every message. Nothing was wrong: that is the documented design working as
written. It puts a second, sharper edge on this task than the O(n) `persist()`
cost that prompted it, because *any* reinstall — a restore, a new phone, a
device-build test like this one — is total history loss with no warning and no
recovery. Not being fixed now (Mike's call, same day), but if this doc gets
picked up, the durability half is the half that has actually bitten.

## Files involved, if/when this gets built

- `ios/Marlow/Marlow/Models/ChatStore.swift` — `persist()`, `load()`,
  `makePersistURL()`, and every call site of `persist()` (there are many;
  it's called from nearly every state transition).
- `ios/Marlow/Marlow/Models/ChatMessage.swift` — the persisted unit; a format
  change touches its `Codable` conformance and whatever migrates old installs.
- `ios/Marlow/MarlowTests/ChatStoreTests.swift` and `TestSupport.swift` —
  several existing tests assert on-disk contents directly (e.g.
  `theTranscriptSurvivesARestart`, `aCorruptTranscriptLoadsAsEmptyRatherThanCrashing`)
  and would need to keep meaning the same thing under a new format.
- `docs/tasks/nightly-consolidation.md` — server-side retention; see the
  coupling note above.

## Options that exist, not a recommendation

Not evaluated against each other — just what's out there, for whoever scopes
this next:

- A real on-device database (SQLite, via `GRDB` or raw `SQLite3`) instead of
  one JSON file — the standard-apps pattern, supports cheap appends and
  windowed loads.
- Sharding the JSON itself (one file per day/month) so a write only touches
  the active shard.
- Something else entirely — genuinely undecided, which is the point of
  writing this down now rather than guessing.

## Decision rule

Don't build this from a worry. Build it when one of these is actually true:

- `persist()` is measurably slow — profile it, don't guess. A rough gut check:
  a `ChatMessage` is maybe 200–400 bytes encoded; even 10,000 of them is a few
  MB, and `JSONEncoder` on a few MB is not slow on any iPhone made this
  decade. This is likely a non-issue for actual years of daily use — the
  point of writing it down is to have the answer ready if it ever isn't.
- Or the visual "scroll through everything" itself becomes the complaint
  (not performance — just not wanting six months of chat above today's).
  That has a much cheaper fix on its own — collapse everything before, say,
  the last 7 days behind a "Load earlier messages" tap, no storage-format
  change required — and the options above are worth revisiting only if that
  isn't enough.
