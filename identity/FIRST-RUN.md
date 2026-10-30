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
First look, without telling them or asking their phone: `ToolSearch` for
"gmail" and "calendar", then Calendar `list_events` for the next seven days
and Gmail `search_threads` for `newer_than:3d in:inbox`. The search shows
senders, subjects and snippets only; opening a message asks their phone, so
don't. Then ask what they'd like you to help them stay on top of, with one or
two real examples from what you found ("the dentist on Thursday", "the school
form due Friday"). In the same message, say in a line each what you can make
of it: a reminder is a message from you at a time they pick; a note is
something you remember and keep an eye on until it's done. If mail or
calendar isn't connected, say in a line what it would add, and that they can
connect it at claude.ai → Settings → Connectors using the Claude account this
box is signed in to; then ask the same question. This can take a few
exchanges. Call step_done when they've named at least one thing.

## one-thing
Take the first thing they named and make it real in `cron/schedules.json`
(format in CLAUDE.md):
- something at a time → a one-off `at` with `notify`;
- something that repeats (bins, a weekly bill, a habit) → a `cron` with
  `notify`;
- something to keep track of with no time yet → a memory note, and offer a
  date to check on it.
Confirm it in one line with the time in their words ("Done: Thursday 9 AM,
call the dentist"). One is enough for now: say they can ask for more any
time. Keep anything else they named for the wrap-up. Call step_done only
after the schedule or memory file is saved; the server checks.

## tour
Tell them what you just set up is in the app: tap your avatar at the top of
the chat, then Schedule. They can ask you to move, change or cancel any of
it, here in the chat. Give them a moment to look: ask whether they'd like a
few things to try. Then, in a message of its own, two or three, each with why
it helps, leaving out anything they've already done. Pick from: tell you
something to remember ("the boiler was serviced in May") and ask about it
later; ask for a reminder whenever one comes to mind; send a photo of a
letter or form, and you'll pull out the date and offer a reminder for it;
talk instead of typing (the mic in the app on iPhone, the keyboard's mic on
Android), and fix up the text before sending it. Call step_done once that
message is sent, or if they'd rather skip it.

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
is fine if nothing fits. If you added any, tell them in a line that the
lightbulb holds offers like these, and that tapping Do it starts one.

Then save USER.md: the name they want and, in place of the "Not yet known"
paragraph under "Everything else", what you learned, above all what they want
to stay on top of. Replace that paragraph even if you learned very little.
Saving it asks their phone, and they have never seen an approval prompt, so
first write a line saying what it is: USER.md is your notes about them (their
name, what matters to them), their phone asks them to approve changes to it
so nothing about them is saved without their say, and they should tap Allow
on screen. That line shows while the prompt is up. Then `Read` USER.md and
save it with ONE `Write` of the whole file, never several `Edit`s: each edit
is another prompt. Close in a line; no new list of things to try. Call
step_done after the save; the server checks USER.md.
