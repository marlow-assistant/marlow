# Chat UX contract

The chat behaviour every Marlow client must reproduce, stated without reference
to any UI toolkit. The iOS app (`ios/Marlow/`) is the reference client; the web
client (`docs/tasks/portable-web-client.md`) is the second. Most rules below are
findings from holding a phone, recorded as comments in `ChatStore.swift`,
`ChatView.swift` and neighbours, or in `docs/tasks/ios-chat-ux-gaps.md`. This
file lifts them out so a builder who has never seen that history does not have
to learn them again.

**Where the iOS code and a doc disagree, the code wins.** Section 16 lists the
disagreements. "Gaps item N" means item N of `ios-chat-ux-gaps.md`; its numbers
are permanent.

## How to read this

Each rule has an id (`SCR-3`), a statement short enough to test, and a *Why*
carrying the finding and its date. Tags:

- **[test]**: becomes a Playwright test that reads element geometry
  (`getBoundingClientRect`, `scrollTop`), never a screenshot.
- **[unit]**: pure logic. Port the iOS test of the same name (section 15) to
  vitest.
- **[device]**: only a real phone with a keyboard up can answer it. Gaps items
  8-10 were each found after a green suite, by holding the phone. Run these on
  Android Chrome, and say so in the PR when it was not run.
- **[derived]**: no device finding behind it. This doc's reasoning about
  browsers, to be checked rather than assumed.

Symbols in backticks are the Swift identifiers the number came from.

Vocabulary: a **message** is one bubble. A **placeholder** is the assistant
bubble standing for a turn in flight. An **exchange** is a user message plus its
placeholder. The **outbox** is every message with no server `seq` yet. A **row**
is a server transcript row.

## 1. The model, and the endpoints the chat uses

The server owns the conversation (`server-transcript.md`). A client holds a
cache of server rows plus an outbox of what it made and the server has not yet
confirmed. Streaming text and activity are not in the transcript; they come from
polling a turn.

| Call | Use |
|---|---|
| `POST /v1/message` -> 202 `{turn_id}` | send. Body: `text`, `client_msg_id`, `images` (`[{b64, mime}]`, at most 6; the legacy `image_b64` + `image_mime` pair is still accepted), `timezone` (IANA), `effort` (`low`/`medium`/`high`) |
| `GET /v1/turn/{turn_id}` | one in-flight turn: `status`, `result_text`, `error`, `activities`, `partial_text`, `partial_text_marks`, `merged_into`. 404 = unknown (restart, or evicted past 256 turns) |
| `GET /v1/transcript?after=\|before=&limit=` | `{rows, latest_seq, has_more}`. Limit defaults to 100, clamps to 500. Neither cursor = newest page, descending. Both = 422 |
| `GET /v1/transcript/attachment/{id}` | image bytes, bearer-authenticated |
| `GET /v1/permissions`; `POST /v1/permission/{id}` `{decision}` -> `{accepted}` | tool approval |
| `GET /health` (no auth, not under `/v1`) | `auth_ok`, `reauth_prewarn`, `queue_paused`, `model`, `effort`, build id |
| `POST /v1/pair` | pairing: a single-use invite from the box (`deploy/pair-device.sh`) for a bearer `device_token` |

There is no `/v1/pending` any more (removed 2026-10-01). It handed each
finished turn out once, globally, which is what the transcript replaced.
Permission requests come from `/v1/permissions`.

Every `POST /v1/message` is recorded with `source: "chat"`, whichever client
sent it, and only chat turns stream (`active_turn.ATTENDED_SOURCES`). Records
stored before the 2026-10 rename carry `"ios"`, so a client that reads `source`
must treat `"ios"` and `"chat"` identically, for good. Only the activity log's
labels read it (`isChatSource` on the web, `TurnSource.isChat` on iOS); don't
branch other client behaviour on it.

## 2. Scrolling

Geometry is in one coordinate space, y growing downward. The **visible region**
is the part of the scroller a reader can see; if something floats over its top,
such as a header, the region starts under it (`readableHeight` in `ChatView`).

**SCR-1. The transcript opens at the bottom.** The first painted frame shows the
last row's bottom at the visible bottom, with no scroll motion after. [test]
*Why:* a cold open landed at the top of a restored transcript, so the moment the
newest message mattered most (tapping a push) was the one it was not shown
(device, 2026-08-07; gaps item 6).

**SCR-2. An arriving row scrolls to the bottom only if the reader is not far from
it.** When the row count changes and `isFarFromBottom` is false, bring the last
row's bottom to the visible bottom. When true, do not move the scroll position at
all. [test]
*Why:* someone reading history must not be yanked. The trigger is a row-count
change, not growth inside the last row.

**SCR-3. A local send always scrolls to the bottom, even from far up.** It is the
one path allowed to override SCR-2. [test]
*Why:* the sender needs to see their own message and its placeholder arrive.

**SCR-4. "Far from bottom" means the content's end is more than
`max(160, visibleHeight / 3)` below the visible bottom.** In a browser:
`scrollHeight - scrollTop - clientHeight > max(160, clientHeight / 3)`, with
`clientHeight` the visible region's height. Until the height is measured, the
answer is "not far". [test]
*Why:* the threshold is from `ChatView`. iOS compares against the whole viewport
instead of the visible region, so its button appears about 100pt later than this
says (gaps item 18, parked; Mike did not notice, 2026-09-29). Implement the stated
rule, not the bug. The "unmeasured means hidden" half is a real finding: deciding
against height 0 made a full transcript clear the threshold instantly, and the
button sat on screen at launch.

### Follow and pin while a reply streams (`ReplyFollow`)

Mike, 2026-09-29: with a long scrollback, a send scrolled to his bubble and the
reply's first line, then the reply ran off the bottom, because the transcript
only scrolled when a *row* was added and a streaming reply grows inside one. The
agreed rule: follow the growing reply until the start of the exchange reaches the
top of the visible region, then stop and let the rest stream in below the fold.

**SCR-5. Following starts at a local send, and only then.** It starts when the
newest two messages are the user message and placeholder this client just added.
It never starts for another device's send or for a briefing, which keep SCR-2.
[unit]
*Why:* following is for the reader who just asked and is watching. A reply
streaming from another device is not followed and grows below the fold; iOS has
not been device-checked on that case.

**SCR-6. The decision.** Inputs: `userTop`, `userHeight`, `replyTop`,
`replyBottom`, `visibleTop`, `visibleBottom`; `tolerance = 0.5`. [unit] and [test]

1. If `visibleHeight <= 0`: hold.
2. `userIsTaller = userHeight > visibleHeight + 0.5`;
   `anchorTop = userIsTaller ? replyTop : userTop`; `span = replyBottom - anchorTop`.
3. If `span > visibleHeight + 0.5`: **pin** (`pinReply` if `userIsTaller`, else
   `pinUser`). Following ends.
4. Else if `replyBottom > visibleBottom + 0.5`: **follow**, scrolling the reply's
   bottom to the visible bottom.
5. Else hold.

Span does not depend on scroll position: scrolling moves both ends together.
Worked cases (visible 100..800 = 700 high; user 60 tall, 12 gap): user top 500
with a 100pt reply holds; user top 600 with 300pt follows; user top 500 with 640pt
pins the user (712 > 700); user top 150 with 628pt still follows (exactly 700);
a 900pt dictation with a 200pt reply follows, and with 710pt pins the reply's top.
*Why:* pinning a user message taller than the viewport would scroll back up and
hide the reply. The 0.5 tolerance is because layout reports fractions.

**SCR-7. A pin lands exactly.** Place the anchor's top at `visibleTop` and stop;
at no sampled frame during streaming may the anchor's top be above
`visibleTop - 0.5`. [test: sample across the whole stream]
*Why:* stopping is a pin, not a last follow step. Otherwise the view overshoots
and snaps back.

**SCR-8. Only growth is news.** Act on a frame only if the span
(`replyBottom - userTop`) changed by more than 0.5 or the visible bounds changed.
The first measurement after following starts is a baseline, not acted on
(`FollowTracker.isNews`). [unit]
*Why:* a scroll moves both rows together. Acting on those frames cut short the
send's own animated scroll.

**SCR-9. Do nothing until the reply has words.** Act only once the placeholder
has streamed text, or is `done` or `error`. [test]
*Why:* row heights interpolating under the arrival animation read as growth, and
following them cut the send-scroll short with a snap (simulator, 2026-09-29).

**SCR-10. Following ends, and does not resume, on:** user scroll input (SCR-11); a
pin; the placeholder reaching `done`, `error` or being removed as merged (after one
more decision on the final layout); or any row added after the placeholder. It
restarts only at the next local send. [test]
*Why:* auto-scroll must never fight a hand. The extra decision matters because the
finished text can be longer than the typing had reached.

**SCR-11. User scroll input ends following the moment it begins, before any
movement.** iOS: the scroll phase reaching `.tracking`. Browser equivalents:
`wheel`, `touchstart`, `pointerdown` on the scrollbar, scroll keys. [test] (the
mapping is mine; section 16)

**SCR-12. Programmatic scrolls while following are instant, never animated, and
never issued during a touch or momentum scroll.**
*Why:* an animated scroll is discarded during deceleration and restarts on every
typed token. Fighting a fling cancels it, which `portable-web-client.md` calls out
as hard on the web.

**SCR-13. Measure the thing you scroll to.** If a timestamp divider sits above the
user message, the exchange's top is the divider's top. [test]
*Why:* measuring only the bubble let the last follow step carry the divider under
the header, and the pin then moved the view back down about 30pt (simulator,
2026-09-29). The cause (`scrollTo(id)` targets the whole row) is iOS-specific, but
the rule is general.

### The jump button and the unseen dot

**SCR-14. The jump button shows exactly while `isFarFromBottom` is true.** A 44x44
circle at the bottom-right, 12pt in. Tapping it scrolls the newest message's bottom
to the visible bottom and clears the dot. [test]
*Why:* gaps item 10 (2026-08-23). Two device findings: with lazy rows the gesture
meant to summon the button evicted the measurement that showed it (iOS only), and a
tap during a momentum glide was lost, so the button looked dead. Check a tap during
a fling on Android. [device]

**SCR-15. The unseen-arrival dot** is a 10pt accent dot at the button's top-right
with a 2pt ring in the background colour. [test]
- Set when an assistant reply completes (`done`) while the reader is far from the
  bottom, whether or not the tab is in the foreground.
- Cleared by tapping the button or scrolling back inside the threshold. Never
  persisted.
- A dot, not a count, and not shared with any unread badge.
- Not set by another device's user row, an error reply, or anything the bootstrap
  fetch loads.

*Why:* gaps item 14 (2026-09-11). The client has no settled answer for whether a
cron turn, a retry and two assistant rows from one turn are one arrival or four, so
a number would be a promise to have one. The app-icon badge means "arrived while you
were away" and survives until the app opens; the dot means "arrived while you were
scrolled up" and dies when you look. Sharing state would make one wrong.

**SCR-16. A permission prompt opening, being answered or timing out does not move
the scroll position.** [test]
*Why:* gaps item 3, open. Mike saw the thread scroll backwards as each of four
prompts cleared (2026-08-06 smoke test). The suspected cause (the request row
sharing an identity space with the message list) is a guess. The web renders prompts
as a modal outside the list; keep it that way.

## 3. Streaming text and the activity line

**STR-1. The source is cumulative.** Each `GET /v1/turn/{id}` while running returns
the *whole* reply so far in `partial_text`. The target only grows; a payload no
longer than the target is ignored. Never rewind a bubble the reader is mid-sentence
in. [unit]
*Why:* `streaming-reply-text.md`. A missed poll costs nothing and there is no cursor
to get wrong. The server caps `partial_text` at 100,000 characters.

**STR-2. The typewriter.** `partialTextTick` = 50ms (since token streaming, 2026-09-03). Each tick reveals
`stride` characters. [unit]
- `stride = max(1, ceil(backlog / 40))`, where 40 = `fastInterval / partialTextTick`
  = 2000 / 50. It is computed **once per arriving slab** and held.
- If the backlog is at least `partialTextJumpThreshold` = 2,000 characters, the next
  tick reveals all of it.

*Why:* slabs arrive as one poll interval of generation (about 430 characters), so
showing each as it lands steps a paragraph at a time. Recomputing the stride from the
shrinking backlog decays geometrically and never finishes, so every reply's tail
arrived late (found while building, 2026-09-01). 2,000 is for a phone back from a
tunnel: show the answer, not perform it. The divisor uses the 2s interval even during
the 500ms burst.

**STR-3. A client joining a turn already under way shows the first slab whole, then
types what arrives after.** This covers a relaunch, a tab reload, and a placeholder
created by sync for another device's turn. It must not apply to a turn watched from
the start. [unit]
*Why:* Mike opened the app on an unread message and watched a finished turn type
itself out (2026-09-05; since fixed). The discriminator is "did this state join
late", not "is this the first ingest", which is also true of a live turn's first
text and would flatten the feature. iOS pins that trap in
`aTurnWatchedFromTheStartIsStillTypedOut`.

**STR-4. Tool-call marks split the reply.** `partial_text_marks` are offsets where a
tool call interrupted the text, counted in **Unicode code points** (the server uses
Python `len()`). Split there, drop whitespace-only pieces, and render each piece as
its own markdown block. Only marks the typing cursor has passed apply. Replace the
stored marks only if the new list is at least as long. In JavaScript split on
`Array.from(text)`, never UTF-16 `.length`. [unit]
*Why:* without it the 11% of turns that go text, tool, text rendered as "I'll check
your calendar.You have three meetings". A shorter list means the server evicted its
oldest marks, and losing one re-fuses two thoughts.

**STR-5. At a terminal status the server's `result_text` replaces everything the
client accumulated.** Discard `partial_text`, typed text and activity state, and
render `result_text` as an ordinary settled message. On `error`, drop the
half-written reply and show `error ?? "Something went wrong."`. [unit]
*Why:* `result_text` is authoritative; the server reassembles the reply's earlier
blocks into it (the whole-reply delivery fix, 2026-09-04, after Mike watched 751 characters replaced by 27).
A finished turn must never keep the approximation.

**STR-6. Streamed text, the activity label and playback state are ephemeral. Do not
persist them.** A reloaded in-flight placeholder resumes as a late joiner (STR-3).
*Why:* persisted text on screen meant the first tick rewound the bubble from a
sentence to a few characters. A persisted label outlived its turn, and a stale label
is worse than none (`ios-turn-activity.md`).

**STR-7. Ingest streamed text and activities only while the placeholder is
`running`,** not `sending` or `stalled`. [unit]
*Why:* `stalled` shows its own words; ingesting kept restarting playback and
republishing the whole transcript for labels nothing could display, for up to an
hour.

**STR-8. A bubble exists only once there is something to say.** A placeholder with
no streamed characters shows only its status line. A turn finishing with no text (a
tools-only turn) gets no bubble, not an empty pill. A failed turn's reason sits
beneath, not in a bubble. A user message always has a bubble. [test]
*Why:* the working indicator, stalled note and error are the app talking, not
Marlow, and a bubble round a spinner is the typing bubble Mike did not want
(2026-09-28).

### Activity labels

**STR-9. The activity line replays a cumulative window as a paced queue.** [unit]
- `activities` is cumulative, oldest first, at most 64 events, `[]` when there is
  nothing to show (including once terminal).
- Find what is new by the longest overlap between the previous window's tail and the
  new window's head. Equality includes `at`; without it a sliding window of repeated
  `Read` events over-matches and swallows new ones.
- Queue a label only if it differs from the last queued one.
- If more than 3 are queued, keep the last 3.
- Show each for at least `activityDwell` = 700ms. The last label stays up until a
  newer one, or the terminal state, clears it.

*Why:* the client samples a stream every 2s. A 0.16s tool leg is invisible to a
latest-value poll, and three events in one poll would flicker as one without the
dwell (`ios-turn-activity.md`, which reversed an earlier "drop the dwell"). Do not
shorten the poll to fix laggy labels; the dwell is the knob.

**STR-10. Label mapping**, first match wins. [unit]

| Event | Label |
|---|---|
| kind is not `tool_start` | `text` gives "Writing…"; anything else "Thinking…" |
| `tool_start`, tool null | "Using a tool…" |
| tool contains `calendar` (case-insensitive) | "Checking your calendar…" |
| tool contains `web` and one of `search`, `fetch`, `browse` | "Searching the web…" |
| tool contains `gmail` or `mail` | "Checking your mail…" |
| `Read` | "Reading a file…" |
| `Write`, `Edit` | "Writing a file…" |
| `Bash` | "Running a command…" |
| `Grep`, `Glob` | "Searching…" |
| `WebSearch`, `WebFetch` | "Searching the web…" |
| anything else | "Using a tool…" |

**Never render the tool name.** MCP connectors choose those names, so they are
semi-trusted text; the name selects a label and never becomes one. An unknown `kind`
is "Thinking…".

**STR-11. Typing hides the generic labels, not tool labels.** Once a placeholder has
streamed text, an in-row indicator whose label is "Thinking…", "Writing…" or absent
is hidden. A tool label stays. [test]
*Why:* Mike, 2026-09-02: "if the client is rendering typewriter text, there's little
point showing a status". A tool label is the exception because in an interleaved turn
the text stops while the model goes back to a tool, and that label is then the only
thing explaining why (`TurnActivity.describesUnseenWork`).

**STR-12. The streaming bubble re-renders alone, and its markdown is parsed
uncached.** Memoise settled text only. [device]
*Why:* at 20 publishes a second, a growing string never hits its earlier cache
entries, and each dead prefix keeps a full parse: quadratic in reply length. On iOS
the whole transcript also re-renders per tick, and scrolling stutters while a reply
types (gaps item 17, parked, unmeasured). In React, subscribe the streaming row, not
the list.

## 4. Status surfaces

Four surfaces, in four places. They must not be merged into one slab.

**STA-1. The presence header** shows what the oldest in-flight reply is doing. Look
only at the last 20 messages (`searchWindow`). Among assistant messages that are
`sending` or `running`, the **oldest** wins: `sending` reads "Sending…"; `running`
reads its activity label, default "Thinking…", which becomes "Writing…" once text is
streaming. `stalled`, `done` and `error` are excluded. With none it reads "Marlow".
The avatar's ring is on whenever it is not idle. [unit]
*Why:* the server runs one turn at a time, so with two queued the older is the one
working; the newest would say "Thinking…" while the real turn searches the web.
`stalled` is excluded because its row explains itself, and a header claiming
"Thinking…" half an hour later is a claim nobody is checking. The header does not
scroll, so the words are visible from anywhere; the in-row indicator only marks
where the answer will land.

**STA-2. The status strip shows four persistent states in this precedence.**
Highest first; tests assert the order. [test]

1. `reauthNeeded`, red: "Marlow needs reauthorization — SSH in and run `claude`."
2. `queuePaused`, orange: "Marlow is paused."
3. `reauthPrewarn`, orange: "Marlow's Claude session expires soon — SSH in and run
   `claude`."
4. `usageHigh`, black on yellow: "93% of this 5 hours used — resets 8:59 PM." Shown
   when a plan window from `GET /v1/usage` is at 90% or more and its reset is still
   ahead (`usageFormat.highUsage`). Usage is read in the health loop after the
   settle: on the first pass of each run, then at most every 5 minutes. A failed
   read keeps the old reading and never touches reachability
   (`usage-visibility.md`).

Show only the top one, plus a "+N" disclosure (44pt-high target) that expands the
rest in place. Nothing can be dismissed, only collapsed. Collapse whenever fewer
than two remain. Pre-warn is suppressed once reauth is needed. The strip sits above
the transcript. [test]
*Why:* gaps item 13 (2026-09-11). Five independent banners could stack and give a
3-second dictation error the same permanent look as a stopped server. A persistent
state is still true a minute later and needs the user to go do something; order
ranks by what they must *do*, not abstract severity. A dismissible reauth strip is
gone the next time it is needed. The collapse reset must be driven by something that
outlives the content, because the last status clearing is the transition that most
needs it. Wording is client-side (facts-on-server); a friend's install will not want
"SSH in".

**STA-3. Key reauth on `auth_ok`, not `status`.** `reauthNeeded = !auth_ok`. Only
overwrite `model` and the build id when a response reports them. [unit]
*Why:* the server reports missing or unparseable credentials as status `"error"`,
not `"reauth_needed"`. Keying on the string left both banners dark in exactly that
case, while the server pushed "session has expired" to the lock screen.

**STA-4. "Can't reach Marlow" is a claim about the network, made only for a
transport failure.** [unit]
- **Transport failure:** a request that never completed. In a browser, a `fetch`
  that rejects with a network error, or that this client timed out itself.
- Any HTTP response, whatever its status, and any body that will not decode, is
  *reachable*.
- A request this client cancelled is reachable too.
- One pass is the transcript sync plus the health call. It is unreachable if either
  hit a transport failure.

*Why:* "can't reach" over a 500 sends the user to check their connection for a fault
on the box. Counting our own cancellation would paint unreachable on every sign-out;
a cancelled loop is one being *replaced*, so its failed calls say nothing. A browser
`AbortError` from our own cancel must be told apart from a timeout we raised
[derived]. A server answering badly produces no indicator at all today, which is a
gap, not a rule (section 16).

**STA-5. The connection indicator** sits at the **foot** of the conversation,
outside the scroller, just above the composer. [test]
- `idle`: nothing.
- `checking`: a small spinner, shown only if the *first* pass of a loop run is still
  running 600ms after it began (`connectionNoticeDelay`). Only that first pass
  announces itself (launch, or foreground after a push tap). A recurring 15s pass
  blinking forever trains the user to ignore the one that matters.
- `unreachable`: an offline icon and "Can't reach Marlow". Sticky: it stays up across
  the quiet gap between passes and clears only when a later pass gets through. A new
  pass never downgrades it to `checking`.

*Why:* gaps item 15 (Mike, 2026-09-07). Tapping "Marlow has something for you" on a
poor connection landed on a transcript with nothing new and no sign anything was in
flight; the fetch had no timeout, so the silence ran to the platform's 60s. 600ms is
two orders over a healthy round trip (about 30ms), so a healthy link never flashes
it. It is at the foot because that is where the eye is after a tap, and outside the
scroller so scrolling to the last row cannot push it out of sight. Only the
sync/health loop writes it: a send reports through its own bubble.

**STA-8. A loop run gives the network 3.5 seconds before claiming it is down**
(`reconnectGrace`). Within that grace a pass that hits a transport failure changes
nothing on screen (the 600ms spinner, if it is up, stays up) and the next pass runs
1 second later (`reconnectRetry`), not 15. The first failure after the grace sets
`unreachable` as STA-4 and STA-5 describe. An indicator already `unreachable` stays
put. Every loop run gets its own grace, launch included. [unit] Web only so far.
*Why:* a phone waking from Doze gets its foreground event before the tailnet is
back, so the wake's first pass failed and "Can't reach Marlow" flashed red for an
instant on every unlock (Android emulator, Mike, 2026-10-02). Real outages last
longer than 3.5 seconds; a wake does not.

**STA-6. Transient notices sit beside what caused them, never in the strip.** Plain
small text, no fill, cleared after 3 seconds. Identify the timer, not the text, so a
second notice is not cleared early by the first's timer. Texts: "Marlow already moved
on — that request was no longer open."; "Couldn't save the photo — sending your
message without it."; "Couldn't find the photo to resend — sending without it." They
render at the foot, between the connection indicator and the composer. [test]
*Why:* gaps item 13. They are the echo of an action the user just took.

**STA-7. An empty transcript is "loading" until the first fetch lands.** With no
messages and no earlier history, show an overlay greeting, "Hi, I'm Marlow.", whose
second line is "Loading your conversation…" with a spinner while no transcript fetch
has ever completed (`awaitingFirstFetch`: the cursor is nil), then "Ask me anything,
or hand me something to keep track of." It is an overlay, not a row, and is never
persisted. [test]
*Why:* after phone-local history was dropped, the greeting flashed before the
server's rows arrived (Mike, 2026-10-01). His call: fine if it is clear a fetch is
under way. If the first fetch can never succeed it stays up beside "Can't reach
Marlow".

## 5. Sending

**SND-1. The message is in the transcript before the network is touched.** On send,
append a user message (status `done`, local id = a fresh UUID) and its placeholder
(status `sending`, `clientMsgId` = that UUID), persist both, then start the POST.
`client_msg_id` is the user message's UUID. [unit]
*Why:* the server dedupes on it, so a resend is idempotent. Its row is permanent for
the 30-day window, unlike the queue's 600s dedup: the transcript records that a
message was sent once, not that it ran once. Compare UUIDs case-insensitively. Sends
are not gated: any number can be in flight, the server serialises them and may merge
queued ones into one invocation.

**SND-2. Per attempt, one 20-second wall-clock bound** (`sendAttemptTimeout`). Race
the request against a timer, treat the timer winning as a transient failure, and also
set the request's own inactivity timeout to 20s. [unit]
*Why:* Mike's call, 2026-08-27: if the POST has not landed in 20s the network is not
there. `/v1/message` returns once the turn is *queued*, so a healthy round trip is
well under a second. An inactivity timeout alone is reset forever by a trickling
connection, the one way the send path could genuinely hang. The deadline is scoped to
the send, so the poll budget is untouched. It must surface as a *timeout* error: a
cancellation error would end the send on the first timed-out attempt instead of
retrying. One flat number covers images, which are about 300KB after downscaling.

**SND-3. Retry policy.** `sendMaxAttempts` = 8. After attempt *n* fails transiently
(n < 8), sleep `min(2s << n, 8s)`: 4s, then 8s six times. Sleeps total 52s, plus up
to 8 x 20s of requests, about 212s (roughly 3.5 minutes). The message stays
`sending` throughout, unchanged on screen. The 8th failure makes it `error`. [unit]
*Why:* a tunnel or dead zone is ridden out so the message **never needs to look
failed** (gaps item 1); the attempt count is deliberately not shrunk. The backoff cap
came down from 30s to 8s on 2026-08-27: there is no contention argument for backing
off, and a long sleep costs dead air after recovery. It stays "Sending…" because that
is accurate; the connection indicator (STA-5) says the network is down. Device pass
2026-10-01: an airplane-mode send sat on its spinner with "Can't reach Marlow", then
landed as one bubble, one reply.

**SND-4. Failure classification.** [unit]
- **Transient (retry):** the request never completed (no connectivity, dropped
  connection, timeout, DNS, connect or TLS failure); any HTTP 5xx; no HTTP response.
- **Permanent (fail at once):** every 4xx (400 bad image or mismatched pair, 401, 413
  oversize, 422); a body that will not decode.

*Why:* retrying a request the server already rejected only delays the same answer. A
5xx is the server's own trouble, and resending is safe because of `client_msg_id`.
Show the server's `detail` for a permanent failure (iOS: "Server error 413: image
exceeds the 6MB decoded limit"). Polling uses a looser rule (POL-5);
`ios-unify-transient-retry-classification.md` records that the two disagree and
leaves the call to an Opus session. Do not quietly unify them.

**SND-5. After a successful POST** set the placeholder's `turn_id`, set `running`,
persist, and poll. A cancelled client (sign-out) must not send one more request after
a backoff sleep.

**SND-6. Manual Retry.** Allowed only on an `error` placeholder whose user message
can be found by `clientMsgId`. Set it `sending`, clear its text, **clear its `seq`
and `answers`**, and resend the same text and stored image under the **same**
`client_msg_id`. [unit]
*Why:* retry covers a send that gave up and a turn that failed (the server enqueues
fresh over an errored turn since 2026-09-28). Clearing `seq` is a 2026-10-01 review
finding: an error row from the server (a failed turn, or the restart's "interrupted"
row) gives the placeholder a `seq`, and the merge only matches replies to
placeholders without one. A retry that kept it would get its reply row inserted as a
*second* bubble while its own poll also completed: two replies, two sounds.

**SND-7. The outbox survives a reload.** Persist messages with no `seq`; on load,
reconcile each: [unit]
- `running` or `stalled` with a `turn_id`: resume polling as a late joiner, label and
  streamed text cleared (STR-6).
- `running` or `stalled`, no `turn_id`: `error`, "Interrupted before Marlow replied —
  please resend."
- `sending`: clear `seq`/`answers` and resend under the same `client_msg_id` with the
  stored image. If the user message is gone: `error`, "Interrupted before sending —
  please resend."
- `done`, `error`: leave alone.

*Why:* the server dedupes, so resending is safe even if the first POST landed.
`stalled` is **not** terminal: persisting it as `error` would strand a recoverable
turn on disk showing "still checking in the background" while nothing checks.

**SND-8. Failed-send display.** Pair a failed reply to its user message by
`clientMsgId`, only when both are in the rendered window. Paired: the user bubble
gets a red circular retry arrow beside it, and the reason is a small right-aligned
red caption under the bubble, pulled up into the row gap. Unpaired (user message
scrolled out, or a cron or other-device turn): the placeholder keeps its own inline
reason, with a Retry only if it has a `clientMsgId`. The error text is copyable.
[test: arrow right of and vertically centred on the bubble; caption below and
right-aligned]
*Why:* gaps item 1; the arrow moved beside the bubble (Mike, 2026-09-28, on
device), the way a send fails in a messaging app. A placeholder with no on-screen
partner keeps its own button so retry is never lost.

## 6. Polling a turn

One sequential loop per turn: **sleep, request, then sleep again if non-terminal.**
Never two requests in flight for one turn.

**POL-1. Three phases.** [unit]

| Phase | Interval | Bound | Permission check |
|---|---|---|---|
| burst | `burstInterval` 500ms | 6s wall clock (`burstDuration`) or 16 attempts (`burstAttempts`), first hit ends it | no |
| fast | `fastInterval` 2s | 150 attempts (`fastAttempts`), about 5 min nominal | yes, once per iteration, before the turn GET |
| slow | `slowInterval` 30s | 120 attempts (`slowAttempts`), about another hour | no |

The phase deadline is checked *after* the sleep, so an expired phase hands over
without firing one more request. If fast ends with no terminal status: set the
placeholder `stalled`, text "Marlow hasn't answered yet — still checking in the
background.", clear streamed text and label, and go slow. If slow ends: `error`,
"Marlow never answered this one. Try sending it again."
*Why (burst):* everything worth showing happens in the first seconds: CLI startup,
the first tool, the first text (3.2s measured). A tool call starting at t=2.1s is
invisible until t=4s on a 2s cadence. Added the evening streaming shipped, 2026-09-03.
Permissions are skipped there because a request has the server's 55s
(`t_hook_seconds`) to be answered, so arriving seconds later is invisible. The slow
phase skips them because by then any request tied to the turn has been dead for
minutes.

**POL-2. The interval is a ceiling on the rate, not a floor.** The loop is
sequential, so the real cadence is `interval + round trip`: about 0.53s on wifi
(30ms) and about 2s on bad cell (1.5s). A slow network can never be polled faster
than it can answer. Do not add adaptive logic. [unit]
*Why:* this is why the burst cannot flood a slow link. It is bounded by wall clock
for the same reason: 12 attempts at 500ms would be 24s of burst when each costs 1.5s.
Do not shorten the 2s interval to fix laggy labels; it doubles `GET /v1/turn` and
`GET /v1/permissions` per in-flight turn, and the cumulative arrays already stop
events being missed.

**POL-3. Terminal handling.** [unit]
- `done`: clear progress, status `done`, text `result_text ?? ""`, queue the turn id
  for receive feedback (FBK-2), persist.
- `error`: clear progress, status `error`, text `error ?? "Something went wrong."`.
- `merged`: remove the placeholder, stop, **no** sound. The server folded this message
  into another turn whose reply answers both.
- Any other status, including unknown ones: keep polling.

**POL-4. A 404 is permanent.** Set `error`, "Marlow lost track of this message (the
server may have restarted). Try sending it again.", and stop. [unit]
*Why:* the server's `_turns` is in memory and capped at 256, so a restart makes the
turn unknowable. The restart's "interrupted" row arrives through the transcript and
replaces this text by the normal merge (SYN-3).

**POL-5. Any other poll failure is transient.** Keep pacing already-queued activity
and text through it, try again at the next interval, and count it against the
attempt budget. [unit]
*Why:* received events are a local playback queue, not a claim about what the server
is doing now; the next cumulative response fills the gap.

**POL-6. Check for cancellation after every suspension point.** A cancel landing
mid-request must not mutate a transcript the UI has walked away from. Sign-out stops
all work.

**POL-7. The transcript sync runs every `healthInterval` = 15s, sync first, then
`/health`.** On the tab becoming visible, cancel the loop and start a fresh one at
once, sync first. One loop, one owner. [unit]
*Why:* after a push wake the transcript is what the user is waiting for, and
`/health` is a banner; each await can take a while on a poor link. Restart rather
than add a second loop: overwriting a tracked task does not cancel it, and a
foreground/background flap orphaned a fetch still using a token the user had just
revoked. Without push, another device's message or a briefing appears within one
interval plus a round trip.

## 7. Syncing the transcript

**SYN-1. The client keeps a cursor and asks past it.** `latestSeq` is the highest row
`seq` held. With no cursor it **bootstraps**; otherwise it **catches up**. Pages are
200 rows. [unit]
- **Bootstrap:** fetch the newest page (no cursor, descending), reverse to ascending,
  set the cursor to the highest `seq` *in the rows* (the page's `latest_seq` only if
  the page is empty), record `has_more` as "older rows exist on the server", apply
  with `announce = false`.
- **Catch-up:** fetch `after = cursor`, apply with `announce = true`, move the cursor
  to the last row's own `seq`, repeat while `has_more`. Stop if a page makes no
  progress, whatever `has_more` says.
- Apply nothing if the sync was cancelled during its fetch. A transcript read
  consumes nothing, so the next sync loses nothing.
- A sync that starts while another is in flight waits for it; it does not skip.

*Why:* the page's `latest_seq` is read a moment after the rows, so a cursor set to it
can skip a row written between; a page that stopped at `limit` ends before it. Waiting,
not skipping: a push tap cancels the loop and starts a successor while the old sync
may still be unwinding, and skipping would drop the sync the tap exists for. An unknown
`role` is skipped but its `seq` still advances the cursor; an unknown `status` is
`error`; every other key is lenient, because a page that fails to decode is retried
forever against the same bytes.

**SYN-2. `announce` is false for the bootstrap and for "Show earlier" pages, true for
catch-up.** It only controls whether a newly landed completed reply is queued for
receive feedback. [unit]
*Why:* a fresh install must not play a receive sound per row of history.

**SYN-3. The merge of rows into the message list** (`TranscriptMerge.apply`) is pure,
processes rows in ascending `seq`, and returns the new list plus effects for the
caller to carry out. A row whose `seq` is already on a message is skipped, so
re-applying a page is idempotent. [unit]

*Local identity wins.* A message this client created keeps its local `id` for life;
the confirming row is folded into it. Only a row with no local counterpart becomes a
new message, with the row's own `id`. **Never change the key of a message already on
screen.** [test: hold a handle to the user bubble's element, confirm it, assert the
same node is still connected]
*Why:* swapping an id is a row teardown, and mid-stream it breaks follow-scroll. In
React the remount would also lose image state and scroll anchoring.

*User row:*
1. If a local user message has `id == client_msg_id`, set its `seq` and change nothing
   else, in particular keep the local image so the photo is not downloaded back.
2. Else if a message already has the row's `id`, set its `seq`.
3. Else insert a new user message (`id` = row id, `createdAt` = row time, `done`,
   the row's `turn_id`). If it has an `image/*` attachment, record its id and request
   a fetch (SYN-7). Then, if **no assistant row in this batch, or already in the
   list, answers `row.turn_id`**, insert a `running` placeholder directly after it
   (no `clientMsgId`) and start a poll for that turn. This is how a reply sent from
   another device streams here.

"Answers" means a placeholder or row whose `turn_id` is that id, or whose stored
`answers` contains it. An assistant message must keep, and persist, the `answers` of
the row backing it.

*Assistant row:*
1. Find the waiting placeholder: an assistant message with no `seq` whose
   `turn_id == row.turn_id`; failing that, one whose `turn_id` is in `row.answers`. If
   found: set `seq`, `answers`, `text`, and status `done` (or `error`, with "Something
   went wrong." if the row's text is empty). Cancel its poll and clear its streaming
   state. Queue receive feedback only if `done`, `announce`, and the placeholder was
   **not already `done`**.
2. Any *other* waiting placeholder whose `turn_id` is in `row.answers` was merged into
   this reply: remove it and cancel its poll.
3. If nothing matched, insert a new assistant message (`id` = row id), queuing
   feedback if `done` and `announce`.

*Why:* `answers` is **not** the row's own `turn_id`. A coalesced reply answers every
message merged into it, and a retry's reply answers the *original* user row rather
than its own fresh turn (`server-transcript.md`). A placeholder whose own poll
finished has already sounded, so the row only confirms it. The row may beat a merged
sibling's poll to `merged`; one reply must not sit under two bubbles.

**SYN-4. Placement is by `seq`, never by arrival and never by clock.** Insert a new
message immediately after the last message with a lower `seq`, or at the start if
none. Messages with no `seq` that already followed that point stay after it, which
puts a cron briefing above this client's unconfirmed send, in server order.
**Exception:** a live placeholder (no `seq`; `sending`, `running` or `stalled`)
directly after that point stays *above* the insert. [unit]
*Why:* on 2026-09-09 Mike unlocked to the morning briefing with the older nightly
consolidate rendered under it, and a 30-minute divider on the older message read as a
lie (gaps item 7). The server is the only writer, so its order is the display order;
rows apply oldest first so sounds queue the same way. The exception is a 2026-10-01
review finding: a row landing mid-stream otherwise sat between a confirmed send and
the reply streaming under it, and follow-scroll treats those two as an adjacent pair.

**SYN-5. Carry out effects in this order:** cancel polls, start polls for new
placeholders (as late joiners, STR-3), queue completions, start attachment fetches.
[unit]
*Why:* a placeholder a row resolved must stop polling before completions publish, or
its poll reaches its own terminal branch and reports the turn done twice.

**SYN-6. Persist the cache and cursor on every change, never per typewriter tick.**
It holds `{latestSeq, hasOlderOnServer, messages}`, outbox included. An unreadable or
old-format cache means start empty and bootstrap. A cold start renders the cache,
then fetches past the cursor. On load drop server-backed messages older than 30 days;
never drop one with no `seq`. [unit]
*Why:* the server prunes at 30 days (`ChatStore.retention`), and an unsent message may
be the user's only copy. iOS rewrites the whole array per event
(`ios-transcript-scaling.md`); use IndexedDB with incremental writes.

**SYN-7. Attachments for rows this client did not send are fetched once with the
bearer header, then cached.** Show only the first `image/*` attachment, inline; the
server marks everything else `Content-Disposition: attachment`. If a fetch fails,
retry once per load, not per tick. [test]
*Why:* a missing attachment would otherwise be requested every 15s for good. An
`<img src>` cannot send a header, so fetch a blob [derived]. The token never goes in
a URL.

**SYN-8. Not handled, on purpose:** a server `transcript.db` reset (the cursor would
sit above `latest_seq`). Parked until it is a felt symptom
(`ios-server-transcript.md`).

## 8. Windowing and "Show earlier"

**WIN-1. iOS builds only about the last 48 hours of rows.** The window starts at the
*earlier* of "48 hours before the reference" and "30 rows before the end"
(`TranscriptWindow.span` = 48h, `minimumCount` = 30), or shows everything if that
reaches the start. It is a date, not an index. [unit]
*Why:* iOS rows are eager (`VStack`), so each is built and its markdown parsed before
the first frame; a cold launch was blank for half a second to a second by 2026-09-25.
Mike's rule (2026-09-06): about 48 hours immediately, older may pause visibly. A date
because a late cron row can land mid-transcript and shift every index. Measured on 575
rows: about 1.4ms per row plus 1.7ms per 1,000 characters (rough).

**WIN-2. The window is a SwiftUI performance workaround; a browser need not copy it.**
What a client must honour: the last 48 hours are present at first paint without a
user action; an arrival older than the window lowers it to include it, so a notified
arrival never lands above a "Show earlier" button (iOS test
`aLateSyncedRowOlderThanTheWindowIsStillShown`); and nothing is pruned. Start without
a window and add one only if a cold-load pause is measured. [device]

**WIN-3. "Show earlier messages" is a tap, not load-on-scroll.** It sits above the
oldest rendered row while older rows exist, locally or on the server. Each tap
reveals one more window step. Once local rows run out and the server reported
`has_more`, it fetches `before = oldest seq`, 200 rows, prepends them with
`announce = false`, and records the new `has_more`, revealing one window step of
them, not the whole page. [unit]
*Why:* row-visibility signals proved unreliable on device. Dropping the whole page in
moves the launch-time pause up the screen.

**WIN-4. Prepending rows holds the reader's position.** The message that was first on
screen stays where it was, top within 1px. [test]
*Why:* rows inserted above leave the offset from the top unchanged, so the message
just read would be pushed a screen down. iOS restores it one run-loop turn after the
tap; for a server page the rows arrive *later*, so the reader may be pushed down. That
is an open iOS bug, **never observed on device** (the server held under 200 rows on
2026-10-01). Restore position after the page lands. [device]

## 9. Feedback

Five events, all fired **unconditionally**: never "only when the device is silent".
iOS cannot read the ringer switch, and it does not matter: the system sound is
suppressed by the switch and the haptic is not, so firing both gives a tone when the
phone can make noise and a tap when it cannot. Do not add the silent-mode check that
looks missing (`ChatHaptics`, gaps item 16).

| Event | Haptic | Fires when |
|---|---|---|
| sent | light | `send` accepted the message locally; before the server confirms; not on a refused send |
| reply started | soft | the first characters of a reply appear on screen |
| reply finished | medium, plus the receive sound | a turn reaches `done` |
| dictation started | rigid | the audio engine is running; not on the mic tap, which precedes two permission prompts |
| dictation stopped | rigid | the audio session is released: mic button, send, or the length cap |

The dictation taps have no sound and are iOS-only (Android dictation is on hold). The
start tap needs `setAllowHapticsAndSystemSoundsDuringRecording(true)`, because a live
`.record` session mutes haptics; that also lets the receive sound play into the mic
when the ringer is on. Both were confirmed on the phone (2026-10-14). [device]

A haptic on *failure* is not built (gaps item 16): held back so the three could be
told apart on device.

**FBK-1. Reply-started fires once per placeholder.** The first typewriter tick that
puts characters on screen announces it; a set of announced placeholder ids keeps it
from firing again however often the text is republished. One tap however many replies
started in the same publish. [unit]
*Why:* Mike's ask (2026-09-11): know the thinking time is over. It fires at the
reveal, not when the slab arrives, since a buzz over a still-blank row is the
unexplained silence this exists to end. The set is **never pruned**, which can only
under-fire; pruning at terminal would re-arm it for a stray late update. It is keyed on
the placeholder's id because a push only announces a *finished* turn, so there is
nothing to match a start against.

**FBK-2. Reply-finished is keyed on the server `turn_id` and fires once per turn.** It
is queued by a poll reaching `done`, a prefetched reply, or a merged row (SYN-3), and
never for `error`, the bootstrap, an earlier page, or a placeholder already `done`.
The consumer drains the queue unconditionally, then plays sound and haptic only if the
tab is in the foreground and the turn was not already announced. [unit]
*Why:* `turn_id` is what a push carries as `ref`, so a turn the system already sounded
can be suppressed (`TurnReadySoundTracker`; `notify` counts alongside `turn_ready`,
review 2026-08-30). Drain unconditionally: a turn that finished while away had its
sound played by the push, and leaving it queued would sound it again at the next
completion. The haptic sits inside the same check, or one event gets two taps. A
prefetched reply must cancel the placeholder's poll first, or it queues the turn twice.

**FBK-3.** Sent feedback and the scroll to bottom (SCR-3) happen together, after the
message is in the transcript.

The web has no push in step 3, so every completion is new and nothing is suppressed;
when Web Push arrives, reuse the `turn_id` keying [derived]. A browser audio context
starts suspended until a user gesture, and `navigator.vibrate` is Android-only
[derived]. Whether reply-started should fire for an in-flight turn that bootstrap finds
on another device is not pinned: iOS fires it, because it is driven by the first
revealed text, not by `announce`. Keep that and do not "fix" it without a device look.

## 10. Composer and images

**CMP-1. Send captures and clears everything staged in one step.** Draft text and
attached image are read and cleared together, and the send carries both or neither. If
`send` refuses, put both back. [test: send with text and image, assert field and
preview are both empty after, and one request carried both]
*Why:* `ios-image-input.md` trap 3: cleared on different steps, a send can carry the
text without the image or the reverse. A draft left behind silently prepends to the
*next* message, one turn later and impossible to attribute.

**CMP-2. The send control shows only when it would do something:** non-blank text after
trimming, or an attached image. An image with no caption is sendable. Text is trimmed
before sending. Neither: refused. [test]
*Why:* the control once gated on text alone, so picking a photo and never typing left
no way to send it.

**CMP-3. Image path.** Up to 6 images per message (`MAX_IMAGES` on both clients, `_MAX_IMAGES` on the server). Each: downscale in the client *before* it
touches the network: long edge 1568px (`ImageDownscaler.longEdge`), JPEG quality 0.6
(`jpegQuality`), EXIF orientation applied, never upscaled. Send as base64 in
`images: [{b64, mime: "image/jpeg"}]`, in the order attached. Encode base64 once, before the retry loop.
[unit]
*Why:* 1568px is where the model's own tiling stops gaining detail. Quality moved 0.8
to 0.6 on 2026-08-27 after a real attachment measured 431KB; that lands near 300KB, not
the 200KB Mike asked for, since 200KB needs about q0.4, which damages the text in
screenshots. The lever for a smaller file is fewer pixels, which reopens the 1568
decision.

**CMP-4. The server's limits:** only `image/jpeg`; at most 6 images (400); at most 6MB
decoded each and 12MB in total (413 over); a pair needs both fields (400); `images` and
the legacy `image_b64` pair together (400); valid, non-empty base64 (400). A bad image is
a **hard** failure: one bad image rejects the whole message, nothing is written, and the
client surfaces it. Never send the caption alone, or with some images missing. [unit]
*Why:* a caption about an image Marlow never saw is worse than an error. The 6MB is a
ceiling against a hostile or buggy client, not what the app sends.

**CMP-5. The preview** is 64x64 thumbnails three across (six make two rows), each with
its own remove control on its corner, above the composer; paste, camera and file pick all append to it, up to the cap. [test]
*Why:* gaps item 11 collapsed Photo Library and Paste into one 44pt "+" menu. Paste
reads the clipboard only when used, never reactively.

**CMP-9. The camera is one tap from "+" on a phone.** On iOS, "+" is a menu with Take
Photo (only where a camera exists), Photo Library and Paste Image. On a touch-first web
device, "+" opens Take photo / Choose photo; Take photo uses a separate file input with
`capture="environment"`. A desktop's "+" opens the file dialog directly. Both feed the
CMP-3 path. [test] for the wiring; [device] for the camera opening.
*Why:* a plain `accept="image/*"` input opens current Android's system photo picker,
which has no camera option (seen on a real phone, 2026-10-23). Desktop browsers ignore
`capture`, so a camera item there would open the same dialog twice.

**CMP-6. If the local save of the image fails, send text-only and say so** ("Couldn't
save the photo — sending your message without it."). Keep the retry copy in the outbox
keyed by `client_msg_id`; a retry re-reads the same bytes, and if they are gone it sends
without them and says so ("Couldn't find the photo to resend — sending without it.").
[unit]
*Why:* what is sent and what is saved for retry must agree, or a retry silently drops
an image the first attempt carried.

**CMP-7. Every message carries `timezone` (IANA) and `effort`.** `effort` is the level
the header shows, sent on **every** message, never sticky, read per attempt so a retry
carries the current choice; default `low`. Settings shows "<Model>/<Effort>" from
`/health`, or the effort alone before the first response; never guess a model name.
(The header chip that showed it was removed 2026-10-12; the ideas lightbulb took its
slot.)
*Why:* an effort change does not preserve the cached prefix, so its cost is paid per
*change*; this is a "settle on a level" control, not a per-message "think harder". A
timezone captured at pairing is wrong after the first flight.

**CMP-8. The input grows from one to five lines, then scrolls.** [test] iOS defines no
Enter rule (Return inserts a newline; send is a button), so the web needs its own
(section 16). iOS had a dictation ordering bug (2026-08-11): the field held
uncommitted text that the dictation session committed *after* the clear, so the turn
sent and the words reappeared. iOS drops focus and defers the read one run-loop turn.
Whether Android keyboard dictation has the same shape is unknown. [device]

## 11. Permission prompts

**PRM-1. Prompts come from `GET /v1/permissions`,** polled once per iteration of the
**fast** phase of an in-flight turn's poll loop, before the turn GET (POL-1). Not in
burst or slow, and not from a separate timer. Publish the first request not yet
expired (`expires_at` is server epoch seconds), or nothing. [unit]
*Why:* a request can only exist while a turn is `running`, so it rides the poll that
exists, at most 2s late. An expired request is never published: a sheet that
opens on one has no false-to-true transition to notice, and sits offering live-looking
buttons. The comparison uses the client's clock, so skew skews the countdown [derived].

**PRM-2. The prompt is a modal card, dismissible only by answering or timing out.**
Redesigned on iOS 2026-10-02 (`PermissionCard.swift`) and ported to the web
2026-10-05. It is centred, never past 60% of the viewport, and the conversation stays
visible around it. Top to bottom:
- a bar that drains from `created_at` to `expires_at` with no number, orange for
  the last ten seconds;
- "Marlow wants to" and the server's `summary.title`, or "Marlow wants to use"
  and the tool name when there is no summary;
- `summary.marlow_says` as a labelled quotation;
- `summary.preview` as diff lines, one line each, cut with an ellipsis;
- the **full** tool input as `key: value` lines sorted by key, plain monospace text,
  as a three-line peek behind "Show details";
- then Deny and Allow, plus "Allow similar for this reply" with "Covers {label}"
  only when `summary.similar` is set.

When the content is taller than the card, it scrolls and the buttons stay pinned.
Buttons arm after 0.5s and stay off while an answer is in flight. A request arriving
while Settings is open pre-empts it, and answering returns there. When the bar reaches
zero the client clears it as a backstop to the next poll. [test]
*Why:* the title and preview are facts the server lifted from the input; `marlow_says`
is the model's claim, which an injected prompt can write too, so it never reads as a
fact. The raw input stays one click away, so the card is a reading aid, never the only
view of what is approved. A ticking "12s left" felt like a bomb timer (Mike). Only
notifications withhold Allow (Review and Deny). Two independent sheets let Settings
silently block a request until the server auto-denied.

**PRM-3. Answering clears the prompt at once.** `POST /v1/permission/{id}`
`{decision: "allow"|"deny"|"allow_similar"}`; on success clear it without waiting for the next poll, and
discard any permissions GET already in flight (a generation counter). If `accepted` is
false, show "Marlow already moved on — that request was no longer open." If the POST
fails, leave the prompt: the next poll still shows it (safe to retry) or has cleared it.
A failed permissions GET is a transient poll failure and never disturbs turn polling.
[unit]
*Why:* the stale in-flight GET would resurrect a request the user just resolved.

**PRM-4. What iOS does with more than one device.** The server is first-answer-wins and
single-use: a second answer returns `accepted: false`. The other device's prompt
disappears **on its next permission poll**, because an empty list publishes `nil`. But:
(a) that poll only runs while that device has a turn in the fast phase, so an idle
device never shows the in-app prompt, and a device in burst or slow does not either;
(b) requests are **global**, not scoped to a turn, so a device polling for its own turn
shows a request raised by another device's turn; (c) nothing in the iOS code withdraws a
lock-screen notification on the other device. Routing is
`multi-device-permission-routing.md`, an unscoped placeholder ("approval spam"). The web
does what iOS does and adds nothing smarter. No doc records a two-device test.

## 12. Rendering

**RND-1. Row layout.** Rows are 12pt apart. A user bubble has a 40pt gutter on its left
and sits right; an assistant bubble sits left and may run the full width. Bubbles
have 14pt/9pt padding, 18pt radius, and are as narrow as their text. The user bubble is
the accent colour at 0.4 opacity; the assistant is the secondary background. An image
sits at the top of the bubble, scaled to fit 220x220, 10pt radius. [test: bubble edges
against the container's edges]
*Why:* the UX-affordances change (2026-09-08) removed Marlow's bubble for width and the transcript read as
a document, so both sides were bubbled again on 2026-09-28 with an asymmetric gutter
(gaps item 12). The assistant side had a 24pt gutter on its right until 2026-10-19:
replies are mostly full-width cards, and on a phone it read as the cards sitting off
centre (the Android user). 0.4, up from 0.15, because at 0.15 on black the user bubble was barely
distinguishable (Mike, 2026-09-29). A new bubble grows from its speaker's bottom corner
(scale 0.92, 0.25s), only for a new last row, so a cold load and "Show earlier" insert
without it; streaming text is not animated.

**RND-2. Timestamp dividers** appear above the first rendered message and above any
message landing more than 30 minutes after the one before. Labels: today, the time;
yesterday, "Yesterday, <time>"; within 7 days (`daysAgo < 7`), "<Weekday>, <time>";
older, "<Mon> <day>, <time>", no year. Computed over the rendered window. The divider is
part of the row it precedes (SCR-13). [unit]
*Why:* a burst of replies minutes apart does not need one each, but a message opened
cold next morning should say when it arrived.

**RND-3. Markdown.** iOS renders fenced code (monospace, scrolls horizontally, selectable,
never wrapped), headings 1-6, bullets (`-`, `*`, `+`), numbered lists, GFM tables and
paragraphs, with inline bold, italic, code spans and links in all of them. No blockquotes
or nested lists (a nested item is flattened). A browser may render more through a standard
parser. [test]
- A **table** has aligned columns (the delimiter row's `:--`, `:-:`, `--:`) and a tinted
  header row. Wider than the bubble, it **scrolls sideways** inside it, like code; it never
  squeezes a word apart. A long cell wraps at a capped width (iOS 220pt, web 14em) rather
  than stretching its column to one line. [test] (Raw pipe text on iOS until 2026-10-14.)
- A numbered list keeps its source start number (`<ol start>`): a list resumed at "4."
  after a paragraph stays at 4 (gaps item 5, fixed 2026-09-25).
- A list item's text **wraps**, never truncates. [test: a long item is taller than one
  line] (Mike's screenshot, 2026-09-21.)
- An unclosed fence while streaming renders everything after it as code. That is the
  natural behaviour, not a bug.

**RND-4. Whole-message Copy** is available on a bubble and on an error line, not while a
turn is in flight (gaps item 2). It copies the raw `text`. It is reached by press and
hold, as on iOS, not by a control on every row: on the web, a `contextmenu` event (a
long press on Android Chrome and Firefox, a right-click, the context-menu key) opens a
one-item Copy menu. On a touch screen the copyable element is not text-selectable, so
the hold cannot also start a selection; with a mouse, a reader who has selected part
of the text gets the browser's own menu instead. [test]
*Why:* the error string is exactly what gets pasted into a debugging session. The web
first shipped a "Copy" link beside every reply; Mike asked for the iOS gesture on the
Android emulator (2026-10-02).

**RND-5. The security rules in `portable-web-client.md` apply to everything here.** Build
the DOM from the parse tree and never set `innerHTML` from model text; link only
`http(s):` and `mailto:` with `rel="noopener noreferrer"`; a strict CSP; permission input
and error text are plain text.
*Why:* replies quote email and web pages, which makes them attacker-influenced input, and
a script in the page can approve tools.

## 13. Hazards a browser adds [derived]

None of these has a device finding behind it. Check, do not assume.

**WEB-1.** Background tabs throttle timers. On `visibilitychange` to visible, and on
`online`, restart the loop at once, sync first (POL-7).

**WEB-2.** `fetch` has no useful default timeout; give every call an `AbortController`
timeout. iOS's sync and `/health` effectively run on the platform's 60s; only the send has
a chosen number (20s). The rest is a decision. The bound covers reading the body, not just
the headers: a link that stalls mid-response must time out like one that never answered
(review, 2026-10-02). [unit]

**WEB-3.** The soft keyboard resizing the viewport must not move a reader at the bottom:
keep the last row's bottom at the visible bottom across keyboard open, close and resize
(`interactive-widget=resizes-content`, `visualViewport`). [device]

**WEB-4.** An image loading after first paint changes row heights. Reserve space, or
re-apply SCR-1 and following once it settles. iOS reads local files and does not have
this.

**WEB-5.** Two tabs share storage, so the outbox and cursor can race. Resends are
idempotent by `client_msg_id` and transcript reads consume nothing, so the failure is
duplicate polling, not duplicate messages. Decide whether to elect a leader tab.

**WEB-6.** A transparent PNG encoded to JPEG through a canvas comes out black; fill white
first. A HEIC file a desktop browser cannot decode should be refused with a message.

**WEB-7.** Do not rely on `overflow-anchor` for stick-to-bottom. SCR-2 and SCR-6 are
measurement plus `scrollTo`.

**WEB-8.** iOS has no `401` handling: a revoked token puts "Server error 401: invalid
device token" in the send bubble while polls and sync fail quietly. The web needs a
re-pair path (section 16).

**WEB-9. The chat asks for notifications itself.** Until push is on, a card between
the header (and status strip) and the transcript says what is missing: "Turn on
notifications so Marlow can reach you while the app is closed." with **Turn on** and
**Not now**. A browser grants permission only from a tap, so the card is the ask;
nothing is requested on load. Not now hides it until the next launch. A refusal turns
it into "Notifications are blocked…" (allow them in the browser's site settings), and
a permission the box could not register into "This browser's notifications can't be
turned on". It is gone while push is on and where the browser has no push. Settings
keeps its own control; the two share one state. The launch-time re-registration feeds
that state too: a box that refuses the subscription shows the "can't be turned on"
card, a later successful registration clears it, and a box that is merely unreachable
at launch changes nothing. [unit] [test]
*Why:* on the Android emulator (Mike, 2026-10-02) notifications were never turned on
in the installed app, so replies arrived with no notification and no badge and
nothing said why. iOS asks at first launch; a control buried in Settings is easy to
forget, and push is how Marlow reaches you at all.

## 14. SwiftUI and iOS workarounds: do not carry these over

- **Eager `VStack` instead of `LazyVStack`, the viewport measured from a `.background`,
  the sentinel row, `InitialBottomScrollAnchor` and its iOS 17 versus 18 split,
  `onPreferenceChange` de-duplication, and `FollowFrameReporter` readers that exist only
  while following.** On device (iPhone 14 Pro, 205 messages, 2026-09-06) a lazy stack
  realised zero rows for eight seconds. All of it makes SwiftUI measure cheaply and
  correctly; a browser reads geometry on demand.
- **Scrolling twice (instant, then again a run-loop turn later) to beat momentum.** The
  *behaviour* (SCR-12, SCR-14) ports; the doubled call may not.
- **The run-loop deferral on "Show earlier"** (WIN-4), and **the focus drop and hop around
  the draft** (CMP-8).
- **The notification service extension, `TurnInbox` prefetch, `PermissionCategory`, the
  app-icon badge.** Push is step 4 of the web plan.
- **`PresenceHeader`'s gradient fade and floating layout.** What matters is that the
  header does not scroll and the visible region starts under it.
- **The window itself** (WIN-2).

What *does* port though it looks like a workaround: stable identity for a message across
its life (SYN-3), measuring what you scroll to (SCR-13), isolating the streaming row's
re-renders (STR-12).

## 15. What to test, and how

**Vitest, porting the iOS tests** (the Swift tests are the spec; same inputs, same
expectations): `ReplyFollowTests` (SCR-6), `StreamedReplyTests` (STR-1 to STR-4, FBK-1),
`TranscriptMergeTests` (SYN-3, SYN-4: a retry after an error row, a cron row above a
pending send, idempotent re-apply, `announce: false`, a placeholder already `done`,
unknown-role tolerance), `TranscriptWindowTests` (WIN-1, if windowing),
`ChatPresenceTests` (STA-1), `FailedReplyPairingTests` (SND-8), and `ChatStoreTests` for
the send, retry and poll state machine against a fake clock and fake API (SND-2 to SND-7,
POL-1 to POL-5, SYN-1, STA-4, STA-5, PRM-3).

**Playwright** covers every [test] rule above, driving a fake server through a streamed
reply and reading `getBoundingClientRect` and `scrollTop`. The ones that are easy to get
wrong: SCR-7 must *sample* `scrollTop` across the whole stream, not check the end state;
SCR-10 must assert that no programmatic scroll is issued after an input event; SYN-3 holds
a handle to a DOM node across the server's confirmation; STA-5 needs a fake clock and a
fake network to prove the 600ms delay and the sticky state.

**Do not oversell Playwright.** Desktop Chromium is not an Android browser with the
keyboard up, and every one of gaps items 8-10 needed a fix after the suite was green. Run
the scroll, keyboard and fling checks (SCR-11, SCR-12, SCR-14, WEB-3, CMP-8) on Android
Chrome, and treat Mike's desktop use and the friends' phones as the real verification.

## 16. Discrepancies, gaps, and judgment calls

**Where code and doc disagree (the code wins):**

1. (Resolved 2026-10-01: `/v1/pending` and `APIClient.getPending` are gone.)
2. `/health` is not under `/v1` and takes no auth.
3. `server-transcript.md` keys sounds and haptics on `turn_id`. That is true of
   reply-finished only; reply-started keys on the placeholder id (FBK-1).
4. `ios-poor-network-timeouts.md` says `gatingSends` holds one send in flight and gives
   ~308s, then ~212s. `gatingSends` is gone (`one-conversation.md`), sends are not gated,
   and the code's number is ~212s (SND-3).
5. `ios-turn-activity.md` says there is no typewriter and no stalled state. Both shipped
   later (`streaming-reply-text.md`, 2026-09-03; `.stalled`).
6. Gaps item 18 describes the jump threshold as a bug. This doc states the intended rule
   (SCR-4); the iOS code still has the bug.
7. `ios-transcript-scaling.md` calls the local file the only durable copy and says
   `ingest` inserts by time. Both are superseded by the server transcript.
9. The brief asks how "first answer wins, the other disappears" works. Only partly
   (PRM-4).

**What iOS does that is not a considered rule, so the web must choose:**

- Nothing handles `401` (WEB-8).
- A server answering sync or health with a 5xx or garbage shows no indicator at all: it
  is "reachable" (STA-4), and no banner says the box is broken.
- No keyboard-send rule exists to copy. Enter sends and Shift-Enter inserts a newline is
  the usual desktop convention; ignore Enter while an input method is composing [derived].
- Which timeout the sync and health calls get (WEB-2).
- Whether to window at all (WIN-2).
- The error text for a send that exhausted its retries. iOS shows the platform's own
  string. Say plainly that Marlow could not be reached, and offer Retry.

**Judgment calls made writing this doc:**

- The browser inputs that end following (SCR-11) are my mapping of iOS's "scroll phase
  becomes tracking".
- The rule ids are new, so a test name can cite one.
- "Far from bottom" is stated against the visible region, not the viewport iOS uses
  (SCR-4).
- Section 13 is reasoning, not findings, and says so.

**Not covered:** the activity, identity and schedules views, dictation, push and the
notification content swap, the app-icon badge, and pairing UI beyond its two endpoints.
`portable-web-client.md` sequences those later.
