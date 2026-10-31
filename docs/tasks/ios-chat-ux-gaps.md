# iOS chat UX gaps — found in use

**This file is the OPEN gaps.** Items that shipped are one line each in
"Closed" at the foot — enough that a code comment citing "§4" still resolves,
and no more. The numbering is permanent and referenced from Swift comments and
other docs, so a closed item's number is never reused and the survivors are
never renumbered. (Consolidated 2026-09-08 on Mike's call: eight shipped
write-ups were diluting the six things still to do.)

These are gaps in the *chat surface*, not the protocol. `ios-minimal-client.md`
covers what was deliberately out of scope for the first build; this covers what
turned out to be missing once it was used daily. Items 1–7 came out of real use
and the August 2026 review of the unsolicited-turns change; 8–10 from continued daily use; 11–14 from a
UI-polish review on 2026-08-22; 15 from Mike thumbing a notification on a bad
connection on 2026-09-07; 16 from Mike on 2026-09-11; 17–18 from the
follow-scroll device pass on 2026-09-29; 19 from Mike on 2026-10-05; 20 from Mike on
2026-10-24; 21–22 from Mike's device pass on 2026-10-31.

**The house lesson from 1–10, worth reading before building any of what is
left:** every one of items 8–10 needed a fix *after* the suite was green, found
by holding the phone. None of those four failures was visible to a test.

## 3. Clearing a tool request scrolls the thread backwards

**Now:** answering a permission prompt appears to scroll the chat back to an
earlier point in the thread rather than holding position at the newest message.
Found by Mike during the 2026-08-06 smoke test, where a single turn raised four
prompts in a row — so the jump happened four times while he was trying to watch
what the turn was doing.

**Want:** clearing a request leaves the scroll position where it was, pinned to
the bottom if it already was.

Likely the permission-request row being removed from (or re-keyed in) the
message list and taking the scroll anchor with it — worth checking whether the
request and the message list share an identity space, and whether the row is
removed or merely restyled once answered. Confirm the actual behaviour on device
before building; the report is from use, not from a trace.

Cheap, and it gets less annoying on its own once the pathless-tool fix lands and
routine turns stop raising four prompts. That is a reason to sequence it after
that fix, not a reason to skip it — a turn that legitimately needs approval is
exactly when the user is watching most closely.

## 5. Numbered lists are renumbered from 1 — FIXED

Found by review of the unsolicited-turns change, 2026-08-07. Fixed 2026-09-25 alongside the
list-item truncation bug (same code): `.numbered` now carries `start`, the
first item's source number, and later items count up from it as in CommonMark.
So a list resumed at "4." after a paragraph stays at 4, and the lazy
"1. 1. 1." style still reads 1, 2, 3. Tests in `MarkdownBlockParserTests`.
Mike confirmed on his phone the same day: the 2026-09-21 truncated reply now
wraps, streamed bullets wrap as they arrive, and a list resumed after a
paragraph keeps its numbering. The truncation fix has no unit test (it only
shows inside the transcript's lazy stack), so that device pass is its only
evidence — see the `listItem` comment in `MarkdownMessageView`.

## 16. Haptics — SHIPPED for chat and dictation, open for errors

Mike, 2026-09-11: he wanted a tap when Marlow *starts* replying and when the
turn finishes, "when the phone is on silent."

**The literal ask is not buildable and did not need to be.** iOS exposes no
public way to read the ringer switch. It turns out to be the wrong question:
`AudioServicesPlaySystemSound` is suppressed by the switch and
`UIImpactFeedbackGenerator` is not, so firing both at each point already
yields "a tone when the phone can make noise, a tap when it can't." That is
what shipped — see the `ChatHaptics` type comment, which exists mostly to stop
the next person adding the silent-mode check that looks missing.

Sent, reply-started and reply-finished are done. Confirmed working on device
(iPhone 14 Pro, `ios-chat-status-haptics` branch, 2026-09-11) — including
that soft and medium are distinguishable, which was the open question the
suite could not answer.

Dictation start and stop taps added 2026-10-14 (iOS-only), felt on
the phone the same day. Contract §9 has the details, including the
record-session opt-in the start tap depends on.

**Still open: a haptic on
failure** — `UINotificationFeedbackGenerator.error` on a send that fails or a
turn that errors. Deliberately left out of the first pass rather than
forgotten: it is the one case where the tap would be carrying news the screen
is not already showing quietly, so it is worth doing, but the three above were
the ask and mixing an unrequested extra one into the same device pass makes it
harder to tell which buzz was which.


## 17. Scrolling stutters while a reply is typing in — PARKED

Mike, 2026-09-29, on device (iPhone 14 Pro, `ios-follow-scroll` branch):
dragging the transcript while a reply types itself in somewhere below is not
smooth. He was careful not to claim a frame rate, and nothing has been
measured.

**Not the scroll-follow change (2026-09-29).** Once a finger lands, `ReplyFollow` stops: its row readers are
removed and it does no per-frame work. The stutter comes from elsewhere.

**Hypothesis to profile first, on the phone:** the typing tick
(`PollingPolicy.partialTextTick`, 50ms since reply text started streaming, 2026-09-03) republishes `ChatStore`
20 times a second. Each publish re-evaluates the whole `ChatView` body over
the eager rendering window (about 48h of rows; eager on purpose, see the type
comment), and the in-flight bubble reparses its markdown uncached
(`MarkdownMessageView(cached: false)`). All of that competes with the drag on
the main thread. The cheapest test that separates the two causes: does the
same drag stutter on a `main` build from before the scroll-follow change? It should, if this is
right.

Two fixes to weigh once measured: isolate the streaming row so the tick only
invalidates that row, not the transcript, or slow the tick while a scroll is
in progress. Neither is designed. The simulator will not answer this;
Instruments on the device will.

## 18. The jump button's threshold ignores the floating header — PARKED

`isFarFromBottom` compares the sentinel's y in the `"transcript"` space with
`viewportHeight`. That space starts at the header's bottom edge (measured
2026-09-29 for the scroll-follow change; see `readableHeight` in `ChatView`), but
`viewportHeight` includes the ~100pt under the header. So the button appears
about 100pt later than its threshold says. This dates from the floating
header (2026-09-28). The fix is to compare against `readableHeight`.

**Mike didn't notice (2026-09-29).** He looked on the phone and found nothing
wrong. The only effect is a few extra lines of scrolling before the button
appears, and there is no "right" spot anyone has a feel for. Kept in the
backlog on his call, to fix eventually.

## 19. The Marlow sheet's icon strip looks unevenly spaced — PARKED

The six panel buttons (`PanelStrip` in `MarlowView.swift`) sit in equal
columns, so their centres are evenly spaced. But "You" and "Soul" are short
labels with narrow symbols, so the empty space gathers around them and they
read as set apart from the rest (Mike, 2026-10-05). It's an optical effect,
and the geometry matches the system tab bar.

**Idea: drop the labels and show icons only.** Without text widths, the
equal columns should look even. The cost is legibility: Activity
(`clock.arrow.circlepath`), Usage (a gauge) and Schedule (`alarm`) are three
similar round symbols, so at least one probably needs a more distinct icon
first. The selected panel's title could move into the navigation bar so you
can still tell where you are. Not started, on Mike's call.

## 20. An unsent draft is lost when the app is killed — PARKED

The composer's text and staged photos are view state only: `@State` in
`ChatView`, and the same in the web `Composer`. A force-quit, or iOS evicting
the app in the background, loses them. Only a message that has been *sent* is
recovered on relaunch (the outbox: a `.sending` message is resent from the
transcript cache and its saved image files). Mike lost a composed message
this way on 2026-10-24 while testing multi-image relaunch. It's nice to fix,
not urgent, until someone types for five minutes and loses it (Mike).

**Both clients.** The likely shape: persist the draft text, plus the staged
JPEGs as files the way a send already saves them, on change or on going to
the background, and restore them at launch. Watch CMP-1: send must still
clear the saved draft in the same step as the on-screen one, or a stale draft
comes back after a successful send.

## 21. The What's New sheet's list ends in a hard edge — PARKED

In `WhatsNewSheet`, the scrolling list of changes is cut off sharply just
above the OK button, so a line can sit half-clipped against it (Mike,
2026-10-31). A short fade at the bottom edge, or a similar effect, would read
better and also hint that there is more to scroll. Only show it while the list
actually overflows, or a short list gets a fade over nothing.

**Both clients.** Check whether the web `WhatsNew.tsx` has the same edge
before calling this done.

## 22. The jump button lands at the end of a long reply — PARKED

The jump-to-latest button scrolls to the newest message with
`anchor: .bottom` (`ChatView`; the web `jump()` in `Chat.tsx` does the same
with `scrollToBottom`), so you land at the *end* of the latest message. For a
long one, such as a morning briefing, the reader then has to scroll back up
to its start (Mike, 2026-10-31). This isn't a regression: the code was
unchanged by the 2026-10-31 `body` split.

**Idea:** when the newest reply is taller than the readable area, land on its
top instead. The scroll-follow code already makes that choice for a reply
arriving live (`.pinReply`, anchor `.top`, near `followReply`), so the
button could reuse it. Keep the 2026-08-23 mid-glide fix (an immediate
scroll plus a deferred repeat), and keep item 18's threshold in mind.
Not started.

## Closed

One line each. The reasoning that outlived the fix moved into the code that
carries it — follow the pointer rather than restoring the write-up here.

| # | Was | Shipped | Where the *why* lives now |
|---|---|---|---|
| 1 | Failed sends looked like nothing happened — auto-retry with backoff, plus the iMessage red-exclamation + Retry affordance | 2026-08-08; Retry moved beside the bubble 2026-09-28 | `ChatStore.submit`/`retry`; `ChatView.failedReplies`; `ios-poor-network-timeouts.md` |
| 2 | Message text could not be copied — whole-message Copy in a context menu | 2026-08-07 | `MessageRow`'s `.contextMenu` |
| 4 | Markdown emitted but rendered as plain text | 2026-08-07 | `MarkdownMessageView` |
| 6 | Cold open might not scroll to the newest message on a long transcript | 2026-09-06 | `ChatView` type comment; `ios-transcript-scaling.md`. It was real, but every predicted mechanism was wrong — **a decision rule ages better than the diagnosis attached to it** |
| 7 | Ingested turns appended at the tail regardless of `createdAt`, so an older briefing could land below a newer exchange (seen 2026-09-09: the nightly consolidate rendered under the morning briefing) | 2026-09-09 | the insert-by-time comment in `ChatStore.ingestUnsolicited` |
| 8 | No send/receive sounds | 2026-08-23 | `ChatSoundPlayer`, `TurnReadySoundTracker` |
| 9 | No app-icon unread indicator | 2026-08-23, plus a follow-up | `APNsRegistrar.marksChatUnread` |
| 10 | No way back to the newest message from far up the transcript | 2026-08-23 | the jump button in `ChatView`, and the sentinel comment above it |
| 12 | Assistant replies sat in gray bubbles, spending width on chrome | 2026-09-08 | Reversed 2026-09-29: both sides are bubbled again, kept wide by a gutter on the user's side only (the assistant's 24pt one went 2026-10-19) — see the `MessageRow` doc comment |
| 11 | Photo Library and Paste sat as two separate composer icons, giving four controls equal weight around a cramped text field — now one 44pt `+` menu | 2026-09-11 | the menu's comment in `ChatView`; add Camera there when `ios-image-input.md` step 2 lands |
| 13 | Five independent banners could stack at the top of `ChatView`, giving a 3-second dictation error the same permanent-looking slab as a stopped server | 2026-09-11 | `ChatStatus` (declaration order IS precedence order) and `StatusStrip` in `ChatView`; transient notices moved beside the control that raises them |
| 14 | The jump button couldn't distinguish "you scrolled up" from "something arrived while you were up here" | 2026-09-11 | `hasUnseenArrival` in `ChatView`. A dot not a count, and explicitly NOT shared with the app-icon badge — different lifetimes |
| 15 | Thumbing a "Marlow has something for you" push on a poor connection opened a transcript with nothing new in it and no sign anything was in flight — `getPending` sets no timeout, so that silence runs to URLSession's 60s | 2026-09-08 | `FetchIndicator` in `ChatView`, `ChatStore.ConnectionActivity`. Deliberately at the FOOT of the conversation, not the banner stack — see item 13 |

## Not in scope here

Streaming, the tool-activity indicator, and perceived latency generally — that
is START's Known gap #4 and has its own settled design (fix at the transport/UI
layer, never by prompting Marlow to say "one moment").
