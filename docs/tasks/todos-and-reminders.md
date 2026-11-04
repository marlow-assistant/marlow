# To-dos and reminders that follow up

**Status: Part 1 built 2026-11-02 (fired reminders reach the model) and
verified on Mike's box the same day:** a one-off `notify` with a phrase the
session had never seen, added by hand to `cron/schedules.json`, fired, and
asked "what did the reminder that just fired say, word for word?" Marlow
quoted it. The entry prunes on firing, so there was nothing left to look up.
The fail-on-main control was not run. Other boxes get it on their next
`update.sh`. Part 2 (a
first-class to-do store that reminders hang off): all three installs
inventoried and all three users answered, 2026-11-04. The sketch is revised and
Mike settled its six decisions the same day, and it was built the same day,
not yet merged or on any box: the server half (store, tools, reminders,
briefing context, migration 0008, identity) and both clients' panes, iOS and
web, on their own branches. Nothing has run on a device or a box. See
"Part 2: as built" below.
Part 3 (Trello as an optional backing store, for shared lists and maybe
projects) is direction only, Mike 2026-11-02; part 2 keeps it possible but
doesn't bend for it (Mike, 2026-11-04).

Raised by Marlow on Mike's box (its `memory/marlow-features.md`,
2026-11-02), after Mike called both failures below a bug.

## The two failures

**1. A fired `notify` is invisible to the model.** `cron_notify`
(`server/app.py`) posts the text through `WorkQueue.post_system_message`,
which writes a transcript bubble with `session_id=None` and never invokes
`claude -p`. That is the design (`schedules-and-reminders.md`: no tokens, no
identity re-injection for a lock-screen string), and the bubble is in the
app. But nothing puts it into the CLI session that `--resume` carries. So
the user reads the reminder inline, replies to it, and Marlow answers a
message whose subject it has never seen. Observed 2026-11-02: a nudge fired
between two turns, the user answered "remind me in 3 days", and Marlow set
the new reminder against the thread it had last discussed.

The identity already admits this in prose ("Only `notify` reminders skip
you — they may reply to one you never saw", `identity/CLAUDE.md`, Scheduled
jobs). Warning the model was the cheap patch; it did not stop the misread.

**2. A reminder fires once and nothing tracks it.** A one-off `at` entry
prunes itself on firing; a recurring one has no notion of done. Whether the
user acted is recorded nowhere, so an ignored reminder is never raised
again. Fired one-offs leave no trace in `schedules.json`, but they do leave
one in the transcript (`source="system"` rows), so the lost ones on a box
can be listed.

Failure 1 is a bug in what exists. Failure 2 is a missing feature, and
patching it into `notify` (acknowledged/unacknowledged states, re-nudge
counters) would build half of a to-do system inside the scheduler.

## Part 1: put fired notices into the next turn's context

Every turn's context (`server/turn_context.py`, `_notices_sentence`) lists
the `source="system"` transcript rows posted since the last turn was built:
time in the user's zone and the text, the newest `MAX_NOTICES` (5), with a
count of any older ones. That covers every server notice, not only `notify`:
the skipped-approval line and the first-run greeting are also bubbles the
model never wrote, and a reply to either has the same problem.

"Since the last turn" is a cursor: the transcript seq read up to when the
last context was built. Kept in memory like the gap. After a restart it falls
back to the newest reply any turn wrote. The cursor is not just "after the
newest reply" because a notice that fires *during* a turn is written before
that turn's reply, and would be buried by it on exactly the turns when the
user is there to answer it (`test_a_notice_fired_during_a_turn_reaches_the_turn_after`).
A failed read costs that turn the sentence and leaves the cursor, so the next
turn still shows it.

Cron `prompt` and `job` turns build the same context, so a job that runs
after a notice is the turn that is shown it. That is right while jobs share
the conversation. Gaps accepted (`/code-review high`, 2026-11-02), each
waiting on a felt symptom: a notice shown only to the nightly consolidate,
whose session then retires, is gone by the morning, as is one shown to a job
that fell back to a fresh session. The cursor moves when the context is
built, not when the turn succeeds, so a turn that then errors uses up its
notices (the gap sentence has the same property). After a restart, a
server-written error row counts as "a turn ran". The restart-interrupted
line is a `system` row too, and is listed with the rest, which is accurate.

Shipped with it: the identity's "they may reply to one you never saw"
became a pointer to the context, and `changes.json` entry 14.

## Questions for the users

1. Where do your to-dos live today: in Marlow, elsewhere, nowhere?
2. What reminders do you ask Marlow for, and what happens when you ignore one?
3. When a reminder isn't acted on, what do you want: nudged again (how
   often?), asked once more, or left alone?
4. Would you open a to-do list screen in the app, or only ever ask in chat?
5. Should Marlow add to-dos it notices (from email, from what you mention)
   or only the ones you ask for?
6. Taylor and Mike share a box with two instances: is a shared household
   list wanted, or is that a different thing? **Answered (Taylor,
   2026-11-02): wanted, with one condition.** She does not want Mike's
   Marlow handing her Marlow things for her to do. Part 3 has the rule
   that follows from it.
7. Do you keep projects in Marlow (longer-running things with several
   steps), and how? Added 2026-11-02: projects may also suit a Trello
   backing store (part 3).

## Part 2: what each install has today (inventoried 2026-11-04)

Each Marlow described its own setup; Mike's box was also read directly.
Shape and role only.

**Mike's box.**
- `memory/todo.md`: one markdown list, an "Open" section of about a dozen
  items. Each is a bold title plus prose: when it was raised, when it is due,
  links to the topic file holding the detail, and its reminder history in
  words ("fired twice, not confirmed done, so ask"). Done items are deleted.
  Marlow is already doing failure 2's bookkeeping by hand, in prose.
- To-dos outside it: "To do" bullets in `notes.md`, "Next" lines and "my
  job is to nudge him" in `projects.md`, a "Reminders" list in a topic file
  the morning brief reads.
- A rule in `memory/preferences.md` routes "add to my to-do list" to
  `todo.md`, plus a one-off `notify` when there is a due time. Others: close
  completed to-dos without asking; same-day errand reminders at 3pm.
- `schedules.json`: three one-off `notify` entries, each matching a to-do
  item by wording only. No id joins them.
- Routines: two daily `job` entries, an evening routine nudge and a daily
  routine block. The block reads a log and replies `[silent]` if the thing
  is already done today.
- Readers of the list: the nightly ideas job cites `todo.md`; the Sunday
  digest has an "Open Loops" section; the morning brief reads a Trello board
  snapshot for one project area.

**Taylor's instance** (her Marlow's report).
- `schedules.json`: three recurring jobs (daily and weekly runs), two that
  together make a repeating daily nag, one one-off `notify`.
- The nag: a job every 15 minutes inside a window. Each run checks the chat
  for that day's stop phrase and replies `[silent]` if it's there; the window
  ends on its own.
- Lists live in about a handful of `memory/` topic files: a running list in
  priority order, a weekly checklist with done and to-do, a parked undated
  idea, dated deadlines. `USER.md` holds one grouped set of admin to-dos
  with a single reminder for the group. Lines are rewritten in place, so
  each file shows current state only.
- Re-raising is judgement, never a timer: in the morning briefing (due
  today or soon), the Sunday week-ahead, the inbox check-in. Closed or
  declined items are noted in memory and dropped.
- Calendar events go on Google Calendar on approval.

**The Android user's box** (his Marlow's report).
- `schedules.json`: five recurring jobs (morning briefing, a week-ahead,
  an evening check, two topic watches). No `notify` entries.
- No list file. Open items sit in `memory/projects.md`; the briefing's
  "waiting on you" section works from it plus the inbox. Offers Marlow could
  carry out are idea cards (`ideas/ideas.json`). Appointments and birthdays
  are in the calendar.
- Re-raising is judgement: waiting items resurface in each briefing until
  cleared; the check-in skips what the conversation already flagged; the
  week-ahead ends with at most one reminder offer.

## Part 2: the answers

| | Mike | Android user | Taylor |
|---|---|---|---|
| 1. Where to-dos live | Marlow; some in calendar entries; projects in Trello | Marlow | Longer-term ones in Marlow, as a reminder that becomes a to-do; today's on paper, crossed out by hand |
| 2. What reminders, what happens when ignored | Lots; an ignored one falls off his plate | Appointments, bills, birthdays, deliveries | The morning nag, until she confirms; "when we're back, remind me to plan X"; these show up in the briefing |
| 3. Not acted on | Re-nudge every 24h until done or "not gonna happen"; snooze, sometimes just a couple of hours | Once a day | Not sure; dislikes feeling micromanaged |
| 4. List screen or chat | Screen, for confidence it is complete; chat sometimes | Screen useful; likes asking | Would open a screen |
| 5. Marlow adds what it notices | Yes, but speculative ones are drafts until confirmed; clear, non-optional ones are real at once | Yes | No: only what she asks for |
| 6. Shared household list | (asked of Taylor) | n/a | Wanted; her Marlow is never handed tasks by his (part 3) |
| 7. Projects | In Trello | n/a | n/a |
| Would hate to lose | The evening routine nudge | Marlow watching mail for bills, appointments, unanswered mail | The morning nag that keeps on until she replies and confirms |

Two more from Taylor, both about tone. The morning briefing is a wall of
text with questions and reminders embedded in it, and it rarely gets a reply.
And after a reminder to book an appointment, her Marlow later asked how the
appointment went, which she had no intention of telling it.

## Part 2: what the three share, where they differ, what must keep working

**Common.**
- No structured store anywhere. State is prose spread across topic files,
  and on all three the to-dos are in more than one file.
- **All three built the same routine shape, independently:** a recurring
  `job` that checks whether today's thing is done and stays `[silent]` if
  so. That is what Mike means by "daily routine". It is not a to-do, and
  works today.
- All three resurface open items in a briefing by judgement. That is where
  things fall off: the judgement drops them.
- All three would open a list screen; two still want to ask in chat.
- Mike's and Taylor's "would hate to lose" are both routines, not to-dos.
- The to-dos people bring to Marlow are the longer-term, dated ones. Today's
  small list stays on paper for at least one user, and that's fine.

**Conflicts.**
- **How hard to push.** Mike and the Android user want a daily re-nudge
  until it's resolved. Taylor doesn't want to feel micromanaged, though her
  one hard nag (the routine) is the thing she values most. So the
  re-nudge is a per-person setting, not a product-wide rule.
- **Whether Marlow adds things.** The Android user: yes. Mike: yes, with
  guesses held as drafts. Taylor: no, only what she asks for.
- **Reminders inside the briefing don't get acted on** (Taylor's wall of
  text). A nudge needs to be short and come with its actions, not a question
  buried in prose.
- **Dated events.** The Android user's reminders are mostly appointments
  and dates that already live in the calendar. Copying them into a to-do
  store makes a second source of truth for something the calendar does well.

**Must keep working (the migration).**
- Every routine `job`, untouched: Mike's two daily ones, Taylor's windowed
  nag (the thing she'd most hate to lose), the Android user's checks and watches. Part 2 does not move them.
- The briefings' resurfacing, now reading the store instead of prose, on
  all three. The Android user's mail watch keeps running and gets a place to
  put what it finds.
- The prose context on each item (why, links to topic files): it becomes the
  item's notes, not a separate file.
- The local rules that route to-dos elsewhere: Mike's `preferences.md` line
  sends "add to my to-do list" to `todo.md`, and it outranks a vague identity
  line. Left in place, the old system keeps being used.
- Every cron prompt that reads a to-do file: Mike's ideas job and Sunday
  digest, the three briefings, the Android user's "waiting on you".
- Leave alone what isn't a to-do: Taylor's weekly checklist (recurring, a
  routine in prose), her priority-ordered running list if her Marlow judges
  it a plan, project detail in topic files, calendar events, idea cards.

## Part 2: the design (decided 2026-11-04)

Native first. Trello stays possible where it costs nothing; where it would
bend the local design, the local design wins (Mike, 2026-11-04).

- **The store is a Marlow-owned MCP server, not a file the agent edits.**
  `weather_mcp.py` is the precedent: a stdio server `invoke.py` hands the
  CLI. Tools along the lines of `todo_add`, `todo_update`, `todo_done`,
  `todo_drop`, `todo_list`. The tools don't touch the file: they call the
  internal API over the Unix socket (`internal_client.py`, as the hook and
  `cron/entrypoint.py` do), and a `server/todos.py` module is the one writer.
  The reasons are native ones: a bad write fails that call with an error the
  model can act on (one invalid entry rejects the whole `schedules.json`
  today, `same-turn-store-validation.md`); ids are assigned by the store,
  never invented; a tap and a turn's write reach the same single writer, so
  there is no race and no ideas-style two-file split. One module for reads
  and writes (Mike, 2026-11-02) falls out of it. The data file lives in its
  own directory, outside `memory/`, which `inject_all` pastes into every
  turn.
- **An item:** id, title, notes, due (optional, a date or a time), state:
  `proposed`, `open`, `done`, `dropped`. "Not gonna happen" is `dropped`, kept
  so Marlow doesn't propose it again.
- **Proposed by source.** Something the user said ("I need to X by Friday")
  is `open` at once. Something Marlow inferred, from mail or anywhere else, is
  `proposed`: never nudged, but never left to sit silently either. When
  Marlow adds one it judges important, it asks in chat then. A proposal
  still unconfirmed after two days is flagged in the next turn's context and
  Marlow asks once more; after that it stays on the list without being
  raised again. On for every user, Taylor included, though she said she'd
  rather Marlow only added what she asks for: none of us knows yet whether
  proactive Marlow is useful or annoying, so the majority path ships and her
  feedback decides whether it's toned down (Mike, 2026-11-04). No opt-out is
  built or written into the identity ahead of that.
- **Done is done.** Marlow doesn't follow up on a closed item ("how did it
  go?") unless the user brings it up. An identity line.
- **Reminder state is per instance and server-owned**, keyed by item id:
  snoozed-until, last nudged, fired-and-unanswered. This keeps the per-person
  nudging rule part 3 needs, and it is also simply where the server's own
  bookkeeping belongs.
- **Reminders are derived, and go through Marlow** (Mike, 2026-11-04). An
  open item with a due time starts a turn at that time with the item in
  context, so the reminder can carry what Marlow knows around it (a reply on
  the thread, a call that came in) and runs inside the session, which also
  closes failure 1 for these. Items with only a date, or none, don't push;
  they're on the screen and in the briefing.
- **The server keeps the guarantee.** If the turn errors or writes nothing,
  the server sends the item's title as a plain `notify`. Marlow can't skip a
  reminder by staying quiet: it skips one only by marking the item done or
  snoozing it through the tool, a state change on the list rather than an
  unseen decision. The identity keeps these to a line or two (Taylor's wall
  of text). Cost: a turn per due item, queued behind a turn in progress.
- **Re-nudge, one fixed rule, a per-person preference on by default:**
  an item that fired and isn't done, dropped or snoozed fires again 24 hours later, until
  it is. Two that come due together can go out as one message, since Marlow
  writes them. Off, the item fires once and then only sits on the list and
  in the briefing. The preference is server-owned, a toggle in Settings and
  a tool Marlow can call, and the identity tells Marlow it exists: a user who
  complains about being nagged gets it switched off, or is told where.
  Snooze: a couple of hours, later today, tomorrow, pick a time. No separate daily "still open" push: the list is where you see
  everything, reminders are what reach you. Batching waits on push count
  becoming a felt symptom.
- **Briefings read the store, not prose:** the server puts the open, overdue
  and proposed items into the morning job's context, so the list it works
  from is fixed. What Marlow says about them stays its judgement.
- **A first-class pane on both clients**, a tappable icon next to the ideas
  lightbulb, badged for overdue and proposed items. `GET /v1/todos` and tap
  endpoints mirror `/v1/ideas`. Sections: open (by due), proposed, recently
  done. Tap done, snooze, drop, accept; tapping an item to talk about it
  starts a turn with the item as context, as accepting an idea does.
- **Routines stay `job` entries** and calendar events stay in the calendar,
  not mirrored. A user who keeps both a to-do and a calendar entry for the
  same thing is fine; nothing designs for it or against it. Recurring
  fixed-text `notify` stays as it is.
- **Migration** ships as an identity change plus a one-off migration job per
  box (`deploy/migrations/`). That box's Marlow moves its to-dos into the
  store: prose context into notes, a matching one-off `notify` becomes the
  item's due time and is removed. It rewrites the local rules and cron
  prompts listed above, replaces `todo.md`'s body with a pointer, leaves
  anything it doesn't recognise alone. Unattended, one-off. Its diagnostic
  output is the point: a report on the box listing every to-do-like line it
  found (source file, what it became or why it was left), plus a summary in
  chat. Mike reads his and Taylor's report over ssh; the Android user reads
  the summary in his own chat.
- Also in the PR: `changes.json` entry, identity update, `/security-review`
  (a new tool surface and a server-owned writer in the identity tree).

Deliberately out: per-item nag policy, lock-screen notification actions,
recurring to-dos (they are routines), priority ordering (only Taylor's
list has one, and she didn't ask for it), shared lists (part 3).

## Part 2: decisions (Mike, 2026-11-04)

1. **Store: an MCP server**, calling the server's single writer through the
   internal API, not a file the agent edits.
2. **Reminders go through Marlow**, with the server's plain-title fallback
   and no silent skip. Each item re-fires on its own; no batched daily
   notice.
3. **Re-nudge is a per-person preference, on for everyone by default.**
   Marlow knows about it and can switch it off when asked, or say where.
4. **Inferred items are `proposed` and don't nudge, but aren't forgotten:**
   Marlow asks when one looks important, and asks once more if it's still
   unconfirmed after two days. The user's own words make an `open` item.
   On for everyone; toned down only if Taylor's feedback asks for it.
5. **Migration is a one-off, unattended job** with a diagnostic report each
   user (or Mike, on his and Taylor's instances) can eyeball.
6. **No calendar mirroring.** A user keeping both for the same thing is
   acceptable, not a feature.

## Part 2: as built (2026-11-04)

Where each decided piece lives, and the calls the design left open.

- **Store:** `server/todos.py`, the single writer. Items in
  `todos/todos.json` in the identity tree (committed and backed up with it,
  Edit-denied: `boundaries.SERVER_OWNED_PATHS`); reminder bookkeeping in
  data_dir `todo_reminders.json`, which the agent can't see. Ids are `t<N>`
  and never reused. A due is a date or an instant; a local time is pinned to
  the user's zone when written, and a past one is refused. Duplicate active
  titles are refused, and so is an inferred re-add of a dropped title.
  Done items are kept 30 days, dropped 180 (so "not gonna happen" sticks).
- **Tools:** `server/todos_mcp.py`, a stdio relay that `invoke.py` adds to
  every call, posts each call to `/internal/todos/tool`; `server/todo_tools.py`
  answers it. A refusal is a tool error with the reason. All nine tools are
  vetted pathless tools, so a cron turn (a reminder) can use them.
- **Reminders:** `server/todo_reminders.py`, a once-a-minute loop in the
  server, not crontab lines: the state is all the server's, and it keeps
  due items out of the schedule store's caps. Items due together share one
  cron-source turn in the conversation. When it ends, the queue asks the
  store which items still need the plain-title notice: none if the reply was
  delivered, else every item the turn didn't change. A reminder queued when
  the server stopped fires again on start. Re-nudge is 24 h after firing,
  per `preferences.renudge`.
- **Proposals:** a proposal two days old is named in the turn context of the
  next chat turn only, once; an unattended turn would spend the one ask unseen.
- **Briefings:** the `morning` and `week-ahead` jobs (by job name, never an
  entry id) get a `[To-do list …]` block in front of their prompt, open items
  with due and overdue, then proposals.
- **API:** `GET /v1/todos` (open and proposed, plus done in the last 7 days;
  dropped never listed), taps `done`, `reopen` (undoes a done; Mike,
  2026-11-04), `drop`, `accept`, `snooze` (an instant
  with offset, at most a year out), `todo_id` on `/v1/message` with a ☑️
  quote, `renudge` in `/v1/preferences`.
- **Talk about it** stages the item as a quote chip above the composer, with
  the field focused, and the next send carries it (typed or dictated words;
  the chip alone sends "About this to-do"). Sending on the tap, as Do it
  does for an idea, was tried first: it spoke for the user before they'd
  said anything (Mike, 2026-11-04).
- **Migration:** `deploy/migrations/0008-todos.py` adds a one-off `at` job
  ten minutes out, on a paired box with the split identity; the shipped
  `cron/todo-migration.md` does the move and writes
  `todo-migration-report.md` beside `USER.md`, then a short summary in chat.
  It never touches a routine. The server puts the last 30 days of chat
  notices in front of that job's prompt (`todo_reminders.migration_block`;
  Mike, 2026-11-04): a fired one-off prunes itself from `schedules.json`, so
  a reminder nobody acted on is otherwise invisible to the move. One that
  names a to-do not on the list and not handled in memory comes over as a
  proposal.

## Part 2: parked (2026-11-04)

Review findings left open at merge: each is an edge case nobody has hit.
Build one when its symptom is felt.

- **A proposal's one ask can be spent unseen.** `reasked_at` is set when
  the turn context is built, so a chat turn that then errors uses up the
  ask, and the proposal is never raised again.
- **Reminder state is never pruned.** Entries in `todo_reminders.json`
  outlive their items. The file grows only with the number of to-dos ever
  made, so it's small.
- **The migration job can outlive its server.** The one-off `at` job is
  queued ten minutes out. A box rolled back to a branch without the to-do
  tools in that window runs the move with no store to move into.

Not seen on a device: accepting a proposal (Mike's trial had only one, and
he dismissed it), and the re-nudge a day after a reminder.

## Part 3: Trello as an optional backing store (direction only)

Mike, 2026-11-02: Marlow needs good to-dos without Trello, so part 2 comes
first and stays the default. Trello is an option on top: a list kept in
Trello instead of the local file gets collaboration "for free", because
both people (and both Marlows) can open the same board, with or without
Marlow. Projects may suit it too.

**Chosen over a Marlow-to-Marlow protocol.** The two instances on one box
are deliberately separate users. A protocol where one writes into the
other's context is a new channel for one person's assistant to put text in
front of another's, and its first use would be the thing Taylor ruled out.
A neutral board both read is not.

**The shared-list rule, from Taylor's condition:** the list is shared;
assignment and reminders are not.

- Either person, or their Marlow when asked, adds to a shared list. Items
  start out belonging to no one.
- An item is someone's only when they take it. Neither Marlow assigns work
  to the other person.
- Only your own Marlow reminds you, on your own settings (part 2's
  per-instance reminder state). Mike's Marlow can tell Mike "Taylor's taken
  that one" and never pushes anything into Taylor's chat.

**The open design question:** who reads Trello. The account's Trello
connector is denied in `deploy/marlow-settings.json`, so today not even a
model turn sees a board. Part 2 has the *server* derive reminders from items,
and the server can't call a connector anyway. Either the server talks to
Trello's API itself (a token on the box, and an egress allowlist entry), or a
turn syncs the board into the local store and the server reads that. If part
2's store is an MCP server, the first is a second backend behind the same
tools. Decide this before building.

Part 2's states don't all map onto a card: `done` is a closed card,
`dropped` an archived one, `proposed` needs a label or a list of its own.
That is part 3's problem to solve, not a reason to drop the state.
