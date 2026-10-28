# Task — two different definitions of "retry this" in ChatStore, design question

**Status:** not started, captured 2026-08-08 from a `/code-review medium` finding
on the iOS resilient-sends change (altitude angle), flagged rather than fixed on the spot because it's
a design call, not a mechanical cleanup — the three findings alongside it
(duplicated lookup, redundant condition, a rebuilt array) were mechanical and
already fixed same-day; this one changes retry *behavior*, including on a poll
path that's been stable for a while. **Suggested for an Opus session** — the
"which direction" question below needs judgment, not just code.

## What exists today, precisely

`ios/Marlow/Marlow/Models/ChatStore.swift` has two independent answers to "is
this network failure worth retrying," for what is conceptually the same
problem — a flaky connection mid-turn:

**`submit()`'s new retry loop** (added this PR, for the initial
`POST /v1/message`) uses `isTransientNetworkError(_:)`, a whitelist:

```swift
private static func isTransientNetworkError(_ error: Error) -> Bool {
    if let urlError = error as? URLError {
        switch urlError.code {
        case .notConnectedToInternet, .networkConnectionLost, .timedOut,
             .cannotConnectToHost, .cannotFindHost, .dnsLookupFailed,
             .dataNotAllowed, .internationalRoamingOff, .callIsActive,
             .secureConnectionFailed:
            return true
        default:
            return false
        }
    }
    if case APIError.server(let status, _) = error {
        return status == 0 || status >= 500
    }
    return false
}
```

Anything not on this list (a decoding failure, an unlisted `URLError` code, a
4xx) fails immediately, no retry.

**`pollLoop`'s existing catch block** (for `GET /v1/turn/{id}`, predates this
PR) uses a catch-all — everything is transient except one named exception:

```swift
} catch APIError.server(let status, _) where status == 404 {
    // Permanent: the server's in-memory turn is gone...
    ...
    return true
} catch {
    continue // transient network hiccup - keep polling until the attempt cap
}
```

A decoding error, for instance, is retried forever by `pollLoop` (up to its
attempt cap) and never retried at all by `submit()`.

## Why this is a real question, not an obvious bugfix

Both designs are individually defensible and were each presumably deliberate
for their call site:

- `pollLoop`'s catch-all is old and has been running in production since
  before this PR. It's proven, and "retry everything but 404" is a simple
  invariant to hold in your head.
- `submit()`'s whitelist is new and narrower on purpose — see its own doc
  comment: retrying a request the server already rejected "just delays the
  same answer." A decoding failure on `POST /v1/message`'s response, for
  example, likely means a protocol mismatch that retrying won't fix.

Picking one design to standardize on means either:

1. **Loosen `submit()` to match `pollLoop`** — simpler mental model, but
   risks retrying things that will never succeed (a persistent decode
   failure now costs `sendMaxAttempts` retries with backoff instead of
   failing in one attempt).
2. **Tighten `pollLoop` to match `submit()`'s whitelist** — more precise, but
   touches poll-loop retry behavior that's been stable and is exercised by
   several existing tests (`transientFailuresKeepPollingUntilTheAnswerArrives`,
   `transientFailuresGiveUpAtTheAttemptCap` in `ChatStoreTests.swift`) —
   changing what counts as transient there could change what those tests are
   actually asserting, not just how they pass.
3. **Leave them different, but document why** — the two call sites have
   different failure costs (a POST retry risks nothing extra since the
   server dedupes on `client_msg_id`; a GET retry that never succeeds just
   burns the poll budget), which might genuinely justify different
   tolerance. If this is the answer, it should be a comment at both sites
   pointing at each other and at this doc, not silence.

## What a session picking this up should do first

Read both mechanisms in full (`submit()`'s catch block and `isTransientNetworkError`
around line 240-325; `pollLoop`'s catch blocks around line 415-484), read the
existing tests referenced above so a behavior change doesn't silently break
what they're pinning, and make a call — this doc is deliberately not making
one.
