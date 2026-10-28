# Ideas panel — offers Marlow could act on, as a first-class surface

**Status: v1 SHIPPED 2026-10-13, both clients, on
Mike's box and phone.** Not yet seen on the Android user's box or on Android. Parked
follow-ups are at the end.
Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

The job now ships in the template (`identity/cron/ideas.md`, installed by
update.sh; scheduled on older boxes by `deploy/migrations/0003-ideas.py`).
Mike's own sources moved to his PREFERENCES.md. The box-only nightly job has
written `memory/ideas.md` since 2026-10-05, and the aging works: every
retirement reason (stale, expired, declined, done) has fired. Step 1's
quality question cannot be answered without the surface (Mike, 2026-10-12):
ideas were never *presented*, only findable by asking. So the surface is the
test.

## The idea

A panel of short offers, each one something Marlow would *do*, not advice for
Mike to act on: a title ("Draft the dinner invites and rank the dates") and a
paragraph saying what Marlow would actually do. One tap and Marlow does it.

**Today's suggestions are not this.** It is easy to think the ideas already
exist and are buried in the consolidations. They are not. `consolidate` only
files memory. The suggestions that do exist are one project line in the
morning brief (on a free stretch) and the weekly digest's "This Week" item.
Both are advice. The "how could I be more useful" review is
`nightly-consolidation.md` piece 3, deliberately deferred and never built. So
this is that piece plus a surface, not a surface over output that exists.

**What makes the offer form better than a digest bullet:** it carries a plan,
so accepting costs one tap instead of a conversation. That is the whole value.
An idea Mike would have to rephrase before Marlow can do it is just a bullet.

## Design (Mike liked the direction, 2026-10-05)

- **One panel, not interaction modes.** No mode bar under the chat. Ideas gets
  a chat-screen icon (see Surface below), not a slot in the Marlow sheet.
  Goal tracking and a social-style feed are separate products. A media/files
  view could later be another panel.
- **A store the agent writes to,** shaped like the schedules store. Each entry
  holds `title`, `plan`, `category`, `source` (what grounded it: a calendar
  event, a memory file, an email), `until` (optional expiry date), `created`
  and `status`. Facts on the server, wording and grouping on the phone.
- **"Do it" sends the plan as a chat message.** It runs through the normal turn
  and the PreToolUse approval path. Nothing new touches permissions, and
  accepting an idea grants nothing.
- **"Do it" is a tap, never a form** (Mike, 2026-10-06). Most ideas need one
  fact first: 6 of 8 on the first night were "Tell me X. I'll …". The card
  does not get an input field, and the phone does not split the title. The
  conversation does that: if the idea needs something, the agent's first
  reply asks for it. That costs one exchange, handles multi-part answers a
  field could not, and keeps the title honest about what it needs.
- **Ideas expire** (Mike, 2026-10-06). An optional `until` date, the last
  day the idea is any use, is set on every live idea that hangs on a date.
  The phone hides an idea once it has passed and highlights it as it nears,
  without waiting on the nightly run. The run then retires it as *expired*,
  separate from *stale*. Titles use dates, not relative days ("Thursday
  Oct 8"), because a card sits on the phone for days.
- **Dismissal is signal.** A dismissed idea stays in the store as a dismissal
  so that generation can read what Mike turns down. This is the defence
  against the failure piece 3 already names: a model asked to be useful from
  a cold start produces plausible filler. Grounding each idea in a named
  `source` is the other defence.
- **Categories come from Mike's life, not a fixed taxonomy.** Let the store
  carry whatever the agent uses, and let the phone group by it.
- **Generation:** a step in an existing job before a dedicated cron. Prune open
  ideas that have gone stale (the event has passed, or the idea was already done
  in chat).

## Step 1: a separate nightly job, no code (running since 2026-10-05)

Mike's call: a job of its own, not a morning-brief section, so it can run and be
judged on its own.

- **What an idea is** (settled 2026-10-05 against screenshots of another
  app's ideas view): a one-sentence first-person offer as the title, plus a
  two-to-four-sentence paragraph of what Marlow would do, what it needs and
  where it stops (anything that sends, books or pays is drafted for approval).
  Two sections: **For you now** (grounded in something live, naming its source,
  up to five) and **Things I can do** (capabilities fitted to his life, not
  tied to this week, up to five, changed slowly). Only offers the tools really
  allow; no logging into his accounts or acting as him.
- **The file had a fixed grammar** so a phone parser could render cards later
  (a heading per idea, a short paragraph, `- key: value` lines, categories from
  a short list because the phone groups by them). It stayed markdown during the
  trial because the app's file viewer only shows top-level `memory/*.md`; the
  built store is validated JSON, migrated from it.
- **`cron/ideas.md`** runs at 00:30 Mike's time. It reads the existing file,
  retires stale, done and declined ideas, adds new ones within each section's
  limit, and keeps the last 20 retired. Every idea must name its source; zero
  new is a normal night.
- **In chat, Marlow acts on the answer.** When Mike says yes or no to an idea
  in chat, Marlow does it and marks it in the file; a decline is recorded in his
  words and is what stops the next run re-offering it. This is separate from
  the panel's dismiss.
- **It replies `[silent]`.** Mike reads the file, not a message.
- **Why 00:30:** named jobs all run in the main conversation. Just before
  `consolidate` retires that conversation, the run sees the whole day as
  grounding and its own context is discarded overnight instead of starting
  tomorrow's session.
- **The question it answers:** would Mike have acted on any? If a few land each
  week, build the store and the surface. If they read as generic, we learned
  that for free.
- **First run (2026-10-05):** 4 For you now and 4 Things I can do, and every
  one followed the grammar. One Things I can do idea was the prompt's own
  example, word for word, so the example has to be one that cannot fit Mike,
  with a line saying never to offer it (the 2026-10-06 revision makes that
  swap and adds `until` plus the dates-not-weekdays rule).
- **In `identity/` since 2026-10-12,** with the v1 panel: without the job,
  every box but Mike's showed an empty lightbulb.
- **Titles read cold (2026-10-12).** A title that asked Mike to pick between
  two options confused him: it asked him to recall a three-week-old decision
  that had since been made. The title is now what Marlow will do, with any
  input it needs in the paragraph, and the stale rule rereads each idea's
  `source`. The "<give me X>. I'll Y." form is gone.

## v1: store, panel, one spotlight a day (Mike, 2026-10-12)

Decided: "do it" goes into the main chat; side chats stay parked as long as
the panel is one tap away. Added: Marlow may actively surface **one** idea,
at most once a day, when it is really good. The point is the occasional "oh
actually, yeah"; it is not a feed. Email grounding is not new scope, since the
job already reads the inbox.

- **Two files, two writers, no write race,** in their own `ideas/`
  directory. Not `memory/`: under `memory_mode=inject_all` every file there is
  pasted into every turn. The agent owns `ideas/ideas.json`, validated like
  `cron/schedules.json` and added to `file_policy.WRITABLE_NAMED_FILES`. The
  server alone writes `ideas/ideas-responses.json`. A write to it by the agent
  is not a vetted write, so it prompts the phone. A tap therefore never edits
  a file a turn might be writing, and the nightly job reads both files:
  dismissals are generation's signal. A refused store gets a turn-context
  warning, as schedules do, because a broken write otherwise shows up only as
  an empty panel.
- **API decisions.** `GET /v1/ideas` returns open ideas only (not retired, no
  response), in the store's order; an absent or refused store returns `[]`.
  `POST /v1/ideas/{id}/dismiss` takes an optional reason and is repeatable, a
  later call replacing the reason: that is how "Not now" stays one tap and the
  reason comes after. `POST /v1/message` gained an optional `idea_id`, and an
  unknown or not-open id is warn-and-drop so the message still sends.
  Transcript rows gained `quote` (`idea_id`, emoji, title) on the user row of a
  Do it tap. The bubble shows Mike quoting Marlow's idea with "Do it" underneath
  (Mike, 2026-10-12); as structure rather than text, it survives a reload and
  needs no markdown in user bubbles.
- **"Do it" is a chat send with `idea_id`.** The bubble shows the title. The
  server records the acceptance and adds one turn-context line (the same path
  as location in `server/turn_context.py`) naming the idea's id, title and
  plan, so Marlow knows which idea and what it offered.
- **Spotlight:** the job may set `spotlight` on at most one idea per run, only
  when it is live, grounded in something new (an email, an event, a change in
  plans) and worth interrupting for. Zero is the normal night. The validator
  rejects more than one. On the phone it is a dismissible card above the
  composer: never a chat message, so it stays out of the transcript and the
  context. It stays until Mike taps Do it, taps Not now, or the next run
  replaces it. **No push in v1:** a push for a mediocre idea trains him to
  ignore pushes. Add push once spotlights have earned it.
- **Panel:** a lightbulb icon in the model chip's old slot, top-left. The chip
  is gone (Mike, 2026-10-12: low value), and model/effort stays visible in
  Settings. Its dot asks one thing: has Mike read the list since
  an idea was `added`? Opening the panel clears it, by day and per device. A
  spotlight does not raise it (Mike, 2026-10-12): the card is the louder
  signal, and a dot that only a decision could clear was noise. It opens over
  the chat as two sections, with the category as a label on each card, since
  grouping inside a section of five or fewer would be mostly headers. Not now
  dismisses at once, and the card collapses to one line offering "add a
  reason". Ideas whose `until` falls in the next two days get a marker, and
  ideas past `until` are hidden on the phone without waiting for the nightly
  run. Fetched on panel open and app foreground. A failed fetch is quiet: no
  error in the chat.
- **Both clients in the same PR series:** iOS and `web/`.
- **Migration:** Mike's box was the only one with the trial file, so there is
  no `deploy/migrations` entry; a hand-run script converted it (retired entries
  became stubs keeping the id and reason).
- **Identity:** `identity/CLAUDE.md` got the store and the spotlight bar in
  the same PR, and Mike's live `CLAUDE.md` was edited by hand. Whether friends'
  boxes also get the nightly job is decided after Mike has used the panel for a
  week.
- **The test, restated:** over two weeks with the panel, does Mike tap Do it
  on a few ideas, and does the spotlight earn "oh actually, yeah" more often
  than a shrug? Not now reasons are the data if it doesn't.

## Dreaming: what comes after step 1

Mike's framing (2026-10-05): use quota that would otherwise go unused to do
useful work ahead of time. Off-peak means nothing on a subscription, but quota
that expires does: a 5-hour window about to reset mostly unused is free
compute, and the usage panel already reads those bars. The order matters:
- **Step 1 tests quality.** More compute behind generic ideas only buys more
  generic ideas.
- **Dreaming buys depth,** once the ideas land. "I could draft an itinerary"
  becomes "I drafted it; want it?"
- **Its trigger is quota, not the clock:** run the ideas job, or do the work
  behind an accepted idea, when the plan bars show slack about to expire.
- **The cost is the main conversation.** Every named job runs in it today, so
  a mid-day dream run grows it. Run dreaming in its own session; the mechanism
  is the side-chat one (`side-chats.md`).
- **Injection:** the job reads email subjects and web pages, and a plan Mike
  taps "do it" on becomes an attended turn. The prompt says never to carry an
  instruction from those into a plan. Revisit that before "do it" exists.

## Surface (Mike, 2026-10-05; build after step 1 proves out)

Replace the model/effort chip on the chat screen (it takes a lot of room and
changes rarely) with two icons:
- **speech bubble:** main vs side chats (`side-chats.md`);
- **lightbulb:** switches the main display into ideas.

The model picker moves into the Marlow sheet. Build the swap when the second of
the two features ships; one icon alone could just replace the chip. **Done
2026-10-12 for the lightbulb:** the chip is removed, and the speech bubble
joins it when side chats ship. This
is why Design above no longer puts it in the sheet, which is full enough
already.

## Parked after v1 (2026-10-13)

Found in review, left until a felt symptom releases them:
- **iOS doesn't re-render at midnight.** An idea whose `until` was yesterday
  stays on screen until something else redraws the panel. Web recomputes on
  each render.
- **A refused local send still takes the spotlight card.** Only when the
  client won't create the message at all: on web, a stopped store (unpaired,
  re-auth needed). A slow or failed network is NOT this case: the bubble
  appears at once and sends, retries and relaunch-resends carry the idea_id
  (tested on both clients). The idea stays in the panel either way.
- **Strip Unicode format characters (Cf) in `ideas._inline`.** Optional
  hardening from `/security-review`: the injected `[Idea: …]` line already
  strips brackets and control characters, and the text is the agent's own.
- **First spotlight not yet seen** on any box. The rule is "most nights,
  none"; if a week passes with none on a busy week, look at whether the bar is
  too high.
