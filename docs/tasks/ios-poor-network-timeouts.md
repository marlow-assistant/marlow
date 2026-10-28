# Task — bound the send path's timeouts so a bad connection fails visibly

**Status: ✅ SHIPPED — merged 2026-08-27.** Corrected 2026-09-25 from "awaiting
review". The real poor-connection acceptance check was never recorded: treat it
as unobserved rather than passed. The acceptance test is Mike sending a message
on poor internet and seeing it land or go red inside a couple of minutes. The
suite going green is not evidence this works: every one of items 8–10 on
`ios-chat-ux-gaps.md` needed a fix *after* the suite was green, and none of the
four failures was visible to a test.

Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

## What Mike saw

Sent a message on poor internet. The row showed **"Sending…" with a spinner** and
stayed long enough to read as hung. He gave up before it resolved, so we don't
know whether it eventually landed or went red. The row said "Sending…", not the
stalled copy, which places this in `submit()`'s POST retry loop and **not** in
`pollTurn`: different budgets and owners, and the whole reason this doc is narrow.

## Why it looked infinite when it wasn't

Bounded, but far past the point a person stops believing it.

1. **`post` could not take a timeout at all.** `get` had a
   `timeout: TimeInterval? = nil` parameter; `post` had none, so every POST,
   including `/v1/message`, fell back to `URLSession.shared`'s default 60s.
   Nothing chose that number for this call site. An oversight, not a decision:
   `permissions(deviceToken:timeout:)` already set the pattern and the reasoning
   (the 60s default "can outlive that budget on a slow connection").
2. **60s is an inactivity timer, not a wall clock.** `timeoutInterval` has
   `timeoutIntervalForRequest` semantics and resets whenever bytes move;
   `URLSession.shared`'s `timeoutIntervalForResource`, the only true wall-clock
   bound, is **7 days**. A connection trickling a few bytes at a time trips
   neither. This is the one path in the send flow that can genuinely hang, and it
   is the poor-signal case, not the clean-disconnect case the retry loop was built
   for.
3. **The retry budget multiplies it.** `ChatStore.submit()` retries up to
   `sendMaxAttempts = 8` with doubling backoff capped at 30s: ~2 minutes of sleeps
   plus up to 8 × 60s of request time is around **ten minutes of "Sending…"**. The
   loop was working as designed; the per-attempt bound under it was never set.
4. **Image sends re-encoded base64 on every attempt**, inside the `while true`.

## Decisions

**The per-attempt timeout is 20s (Mike, 2026-08-27):** if the POST hasn't landed
in 20 seconds the network isn't there, and he'd rather see that than a spinner.
`/v1/message` returns as soon as the turn is *queued*, so a healthy round trip is
well under a second. It lives as a named constant on `PollingPolicy` with the
reasoning attached.

**One flat number covers image sends too, deliberately.** `ImageDownscaler`
shrinks every attachment on the device (long edge 1568px, JPEG q0.6, decided
2026-08-11 for exactly this dead-zone case); `_MAX_IMAGE_BYTES = 6MB` is a
server-side ceiling against a hostile client, not what this app sends. A few
hundred KB isn't meaningfully slower than a text body on any connection that
works, so scaling the timeout by payload would guard a case this client can't
produce. **Revisit only if the downscaler's target grows substantially.**

**Keep `sendMaxAttempts = 8`.** Shrinking it was a trade made in
`ios-chat-ux-gaps.md` item 1 and isn't what this revisits; the loop is what rides
out a tunnel. The *interval* cap was never part of that trade. The first draft
claimed the 20s bound put the worst case "near four minutes"; that was wrong: 8 ×
20s plus sleeps of 4+8+16+30+30+30+30 = 148s is **~308s, over five minutes**, the
four 30s tail sleeps nearly half. **`sendMaxInterval` comes down from 30s to 8s
(Mike, 2026-08-27).** There is no contention argument for backing off (one phone,
one box, and `gatingSends` held one send in flight at a time), so the schedule
only stretches a fixed attempt count over a useful wall clock. A long sleep pays
for coverage in latency *after* recovery: the thing retried is a single boolean
(is there a network path yet), so with a 30s cap signal can return a second into a
sleep and the message still sits dead 29 more, indistinguishable from the hang.
At 8s the worst case drops from ~308s to ~212s and post-recovery dead air is
bounded at 8s.

**Not `NWPathMonitor` / `waitsForConnectivity`**, the obvious "stop guessing when
the network is back" answer and the wrong one here: they fix the clean-disconnect
case the loop already handles and do nothing for a *trickling* connection, where
the path stays `.satisfied`, which is precisely what produced the report.

**The wall-clock bound that inactivity timeouts can't dodge is an async deadline
around the single call in `submit()`** (race the request against a `Task.sleep`,
cancel the loser). Rejected: a custom `URLSessionConfiguration` with
`timeoutIntervalForResource` replacing `URLSession.shared`, which bounds *every*
call including the poll loop, a wider blast radius than this bug needs, and the
poll loop's long budget is deliberate (`chat-poll-slow-phase.md` is separately
arguing about it). The code notes why the configuration route was passed over, so
nobody "simplifies" it back.

**The likeliest way to get this subtly wrong:** `URLError.timedOut` is already on
`isTransientNetworkError`'s whitelist, so a deadline expiry should retry rather
than fail; but a `CancellationError` is *not* on it, and if the deadline surfaces
as one it fails the send immediately. The deadline is built to surface as
`URLError.timedOut`.

## What was built — 2026-08-27

`APIClient.post`/`sendMessage` take an optional timeout (existing callers keep the
session default; `ChatStore` supplies the 20s for text and image sends). Each
attempt races the request against a structured wall-clock deadline, the loser
cancelled in either direction. Base64 is encoded once before the loop.
`sendMaxInterval` 30s → 8s. Four Swift tests (timeout plumbing, deadline expiry
retrying as transient, the same bound on images, cleanup when the request wins):
Swift suite 135 → 139, Python suite 818 passing.

## Explicitly out of scope (still someone else's call)

- **What the UI shows during a long send.** Whether "Sending…" should become more
  informative after N seconds is a UX judgment on the chat surface.
- **`ios-unify-transient-retry-classification.md`:** `submit()` and `pollLoop`
  hold two definitions of "worth retrying"; that doc wants an Opus session and
  isn't making the call.
- **`chat-poll-slow-phase.md`:** the poll budget carries Mike's own instruction,
  *"I don't want to add engineering to this path."* The deadline is scoped to the
  send loop so it doesn't touch it.
