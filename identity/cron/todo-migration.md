To-do move. This runs once. The server now keeps a to-do list of its own: the
app shows it as a screen, reminders come from it, and the briefings read it.
Your job in this run is to move the user's existing to-dos into it, so the old
places stop being used, and to write down exactly what you did.

You change the list only with the to-do tools (`todo_list`, `todo_add`,
`todo_update`, `todo_done`, `todo_drop`); the file under `todos/` is the
server's, and editing it is refused. Everything else here is Read, Edit and
Write on your own files. Nobody is awake to approve anything, so a refusal is
the situation, not a decision they made. Don't commit or push; that happens
automatically. Today's date and their timezone are in the context line at the
top of this prompt.

Start with `todo_list`. If items are already there, this has partly run
before: don't add a title that is already on the list.

---

## 1. Find them

Read every file under `memory/`, `USER.md`, `PREFERENCES.md`, and
`cron/schedules.json`. A to-do is a one-off thing the user means to do, or
asked you to keep track of, that isn't done yet: a dedicated list file, "To
do" or "Next" bullets in a notes or projects file, a reminders list in a topic
file, an admin chore with a deadline.

These are **not** to-dos. Leave them exactly as they are, and say so in the
report:

- **Routines.** Anything recurring, above all a `cron` entry in
  `schedules.json` that runs a job or a prompt: a daily check that replies
  `[silent]` when the thing is done, a nag inside a window. Never change,
  move or remove a `job`, `prompt` or recurring `notify` entry. A recurring
  checklist in a memory file is a routine too.
- Calendar events, idea cards (`ideas/`), and project plans or detail. A
  project's next step can be a to-do; its plan is not.
- Done or declined items. Don't carry them over.

When you can't tell, leave it where it is and list it in the report as left,
with why. A missed item the user can add; a routine broken by this run is
much worse.

## 2. Move each one

`todo_add` with:

- **title:** one line, in their words where you have them.
- **source:** `user` when they asked for it or told you about it, which is
  most of them. `inferred` only for something you noticed yourself (from mail,
  say) and they never confirmed; it lands as a proposal.
- **notes:** the prose context that went with it: why, what was said, which
  topic file holds the detail ("details in memory/house.md"). Not its
  reminder history.
- **due:** a date (`YYYY-MM-DD`) or a time (`YYYY-MM-DD HH:MM`, their local
  time) when the item has one. Look for a matching one-off `notify` entry in
  `schedules.json` (an `at` entry whose text is this item's reminder): its time
  is the item's due time. Once the item is added with that time, remove that
  `notify` entry; the list reminds them now, and two reminders for one thing is
  noise. A due time already past is refused: give the date alone if it is
  today or later, otherwise no due, and say in the notes when it was due.

Keep `schedules.json` valid: change only the one-off `notify` entries you
moved, and leave every other entry and field as it was.

## 3. Point the old places at the list

In this run you can write only inside `memory/`, `cron/schedules.json` and
the report; anything else would need their approval, and nobody is there to
give it. So:

- **Rules in a memory file.** A line that sends to-dos somewhere else ("add
  to-dos to todo.md", "set a reminder when there's a due time") outranks the
  identity, so the old system would keep being used. Rewrite it to say to-dos
  go on the to-do list through the to-do tools. Keep the rest of the rule if
  it says something else too (when to close one without asking, the hour for
  errand reminders).
- **`PREFERENCES.md` and job prompts** (a `cron/*.md` that reads a to-do
  file: a briefing, a digest, a check-in, the "waiting on you" part of a
  brief): don't try to edit them. Write the exact change each needs in the
  report, and offer it in your summary; when they say yes in chat, make it.
  A briefing already gets the list in its context; any other job can call
  `todo_list`. Prompts that are the software's are already updated.
- **The files themselves.** A file that was only a to-do list: replace its
  body with one line, "Moved to the to-do list on <date>; use the to-do
  tools." Elsewhere, remove a bullet you moved when the bullet is only the
  to-do, and leave prose that says more than that.

Anything you don't recognise, leave alone.

## 4. Report

Write `todo-migration-report.md` at the top of the identity tree (beside
`USER.md`, not in `memory/`). Its point is that someone can check this run
without redoing it. List every to-do-like line you found, grouped by the file
it was in, each with what it became (`t3`, its title, its due) or that it was
left, and why. Then every rule, prompt and `schedules.json` entry you changed
or removed, with a short before and after, and the changes you are
proposing to `PREFERENCES.md` and job prompts. Plain markdown, no preamble.

Then reply with a short summary for the chat, three or four lines: how many
items are on the list now, anything with a due date coming up, what you left
where it was, the rule or prompt changes you'd like their OK for, and that
the full report is in `todo-migration-report.md`.
Never `[silent]`: they should know this happened.
