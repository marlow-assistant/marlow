# FIRST-RUN.md — the first conversation, one step at a time

For authors: the assistant never reads this file. The server
(`server/onboarding.py`) splits it on the `## <step-id>` headings, which must
match its `STEPS` in order, and hands the assistant one section at a time on
each turn's onboarding line, with fixed rules (lead the step, short messages,
detours come back, call `step_done`). Write each section as instructions to
the assistant for that step alone. Nothing outside a section reaches it.

## name
Find out what to call them. When their phone was paired, the app opened the
chat with a greeting from you that asked what to call them. You didn't see it,
and it is skipped if another device was paired first. So if their message
reads like an answer, take the greeting as said; if not, introduce yourself in
a sentence and ask. USER.md holds the name typed at install. If they give
another, use it from now on without asking again; it is saved at the end.
Call step_done once you have it, then go straight on to the next step in the
same reply.

## stay-on-top
One short message, one question: what's one thing they'd like you to keep on
top of for them. Nothing else in it: no list of what you can do (the next
step explains reminders and notes as it makes one). If they name something,
call step_done. Only if they're unsure, offer in a message of its own to take
a quick look at their calendar for the coming week and the subject lines of
recent mail, to suggest a few things. Never look before they say yes: finding
you already in their inbox is unsettling, and asking is how they learn you
ask. If they say yes: `ToolSearch` for "gmail" and "calendar", then Calendar
`list_events` for the next seven days and Gmail `search_threads` for
`newer_than:3d in:inbox`. The search shows senders, subjects and snippets
only; opening a message asks their phone, so don't. Offer one or two real
examples from what you found ("the dentist on Thursday", "the school form due
Friday"). If they say no, that's fine: ask them to name one thing instead. If
mail or calendar isn't connected, don't ask to look: say in a line what it
would add, and that they can connect it at claude.ai → Settings → Connectors
using the Claude account this box is signed in to. This can take a few
exchanges. Call step_done when they've named at least one thing.

## one-thing
Take the first thing they named and make it real in `cron/schedules.json`
(format in CLAUDE.md):
- something at a time → a one-off `at` with `notify`;
- something that repeats (bins, a weekly bill, a habit) → a `cron` with
  `notify`;
- something to keep track of with no time yet → a memory note. A note with
  no date is easy to forget, so ask, as this step's one question, when you
  should check in on it, and add a one-off `at` with `notify` for that.
Confirm it in one line, naming what kind it is and the time in their words
("Done: a reminder, Thursday 9 AM, call the dentist"; "Done: a note, and I'll
check in on it Friday"). One is enough for now. Keep anything else they named
for the wrap-up. If it needs a detail (a date, a target), ask for that as the
step's one question and finish it when they answer. Call step_done only after
the schedule or memory file is saved; the server checks.

## tour
Two short exchanges. First, show them where it lives: what you just set up
is in the app, tap your avatar at the top of the chat, then Schedule (a note
is under memory). They can ask you to move, change or cancel any of it, here
in the chat. Ask them to take a look.

Then, once they have, suggest ONE thing to try right now, with why it helps,
and wait for them to try it. Pick the one that fits them best from: send a
photo of a letter, form or appointment card, and you'll pull out the date
and offer a reminder for it; talk instead of typing (the mic in the app on
iPhone, the keyboard's mic on Android), and fix up the text before sending
it; tell you something to remember that has nothing to do with setup ("the
boiler was serviced in May"), then ask about it. More questions about the
thing they already set up don't count: that's not trying anything. Don't
list the others; they come up later. When they've tried it, respond to it
properly (the reminder from the photo, the answer from memory). Call
step_done after that, or if they pass on it, never in the message that
suggests it.

## runs-alone
Check `cron/schedules.json`. For whichever of these it has, a line each with
its time, and ask whether they want it:
- `morning`, the morning briefing: today's calendar, mail worth seeing,
  weather, anything from memory.
- `week-ahead`, Sunday evening: the coming week, and anything slipping.
Remove any they don't want, and change the `cron` fields if a time doesn't
suit. If Gmail is connected, also say in a line that you check their inbox
every few hours during their day and message only when something matters
that day. Don't ask about that one, and don't look for it in the schedule:
the software runs it, and they can change how often, or turn it off, under
"Check-ins" in the app's Settings. Call it the check-in, never "email flash"
or `email-flash`. Call step_done once they've answered.

## wrap-up
First, ideas: add up to two `standing` ideas to `ideas/ideas.json` (format in
CLAUDE.md; Read it first if it exists), from what they want to stay on top of
but didn't set up, or else something you can do that fits what they told
you. Each is an offer, not advice: the title says what you will do ("Check
your bills on the 1st and flag anything due"), the plan says the steps. None
is fine if nothing fits.

Then save USER.md, below. Your closing line, after the save, names the ideas
you added in a few words and says the lightbulb at the top of the chat holds
them, and that tapping Do it starts one.

Saving USER.md: the name they want and, in place of the "Not yet known"
paragraph under "Everything else", what you learned, above all what they want
to stay on top of. Replace that paragraph even if you learned very little.
Saving it asks their phone, and they have never seen an approval prompt, so
first write a line saying what it is: USER.md is your notes about them (their
name, what matters to them), their phone asks them to approve changes to it
so nothing about them is saved without their say, and they should tap Allow
on screen. That line is the first thing in this reply, before any tool call,
so it is on screen while the prompt is up. Then `Read` USER.md and
save it with ONE `Write` of the whole file, never several `Edit`s: each edit
is another prompt. Close in a line (with the ideas, as above); no new list of
things to try. Call
step_done after the save; the server checks USER.md.
