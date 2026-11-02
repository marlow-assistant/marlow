# To-dos and reminders that follow up

**Status: proposed 2026-11-02, nothing built.** Part 1 (fired reminders
reach the model) is ready to build and needs nobody's input. Part 2 (a
first-class to-do store that reminders hang off) waits on input from Taylor
and the Android user, because three boxes already have reminders live and
none of us can see what the other two Marlows have built on top of them.

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

When a user turn starts, `turn_context` adds the system notices posted since
the model last ran: time and text, newest last, capped (a handful; a backlog
past the cap says how many were dropped). Read them from the transcript, not
an in-memory list, so a restart between the fire and the reply loses
nothing. "Since the model last ran" is the newest non-system turn in the
transcript; cron `prompt` and `job` turns already run in the session and are
not repeated.

Test first: a notice posted, then a user message, and the turn's context
names the notice; a second message does not repeat it; a restart in
between still carries it.

Same PR: the identity line becomes what the context now shows, and a
`changes.json` entry ("Marlow now sees reminders it sent you, so you can
reply to one"). No client change, so both clients have it by construction.

Open edge, not worth machinery yet: a notice that fires *during* a turn
lands after that turn's context was built, so it reaches the model one turn
late. Note it; fix it if it is observed.

## Part 2: what is live today, and why we ask first

Every box got the same primitives: `notify`, `prompt` and `job` entries in
`cron/schedules.json`, and free-form files under `memory/`. What each Marlow
built from them is local and unrecorded:

- **Mike's box:** `memory/todo.md`, a markdown list Marlow keeps by its own
  convention; one-off `notify` entries for dated nudges; the nightly ideas
  job cites `todo.md` as a source. The list and the reminders never connect.
- **Taylor's instance and the Android user's box:** unknown. Their Marlows
  may keep a list file, may use `prompt` entries as reminders (those *do*
  run in the session, so they already avoid failure 1 and could already
  follow up), or something else.

That has two consequences. A store that ignores what is there leaves two
parallel systems on each box, and the old one keeps working, so it keeps
being used. And no migration can be written blind: Mike can't read Taylor's
instance without sudo or the Android user's box at all. Whatever ships has
to be adopted by each box's own Marlow, from an identity change plus
something like `deploy/migrations/`, and it should leave anything it does not
recognise alone.

So the first step is an inventory by each Marlow of its own conventions,
described by shape (file names, entry kinds, how it decides to nudge),
never content. Then the questions below. The answers go here, summarised
by shape and role.

## Questions for the users

1. Where do your to-dos live today: in Marlow, elsewhere, nowhere?
2. What reminders do you ask Marlow for, and what happens when you ignore one?
3. When a reminder isn't acted on, what do you want: nudged again (how
   often?), asked once more, or left alone?
4. Would you open a to-do list screen in the app, or only ever ask in chat?
5. Should Marlow add to-dos it notices (from email, from what you mention)
   or only the ones you ask for?
6. Taylor and Mike share a box with two instances: is a shared household
   list wanted, or is that a different thing?

## The sketch being asked about (not agreed)

Marlow's, refined 2026-11-02. Recorded so the answers have something to push
against; nothing here is decided.

- **One structured store** in its own directory (not `memory/`, which
  `inject_all` pastes into every turn), agent-owned and validated like
  `ideas/ideas.json`. An item: id, title, optional due date, state (open,
  snoozed until, done).
- **Two writers, two files**, the ideas split: taps in the app (done,
  snooze) go to a server-owned responses file, so a tap never races a turn.
- **Reminders are derived, not written.** The renderer that owns the
  crontab computes nudges from open items. Re-nudge is the server's job;
  otherwise "keep at it until I've done it" depends on Marlow remembering to
  reschedule, which is failure 2 again.
- **A fired reminder is a state change on an item**, so Part 1's context
  line names the item and a reply resolves against it.
- **A list screen on both clients**, the ideas panel as precedent.
- `todo.md` and one-off `notify` entries fold in; recurring fixed-text
  `notify` ("bins out tonight") probably stays as it is.

Deliberately out until the answers ask for it: a per-item nag policy (one
fixed rule first), shared lists across instances, notification actions on
the lock screen.
