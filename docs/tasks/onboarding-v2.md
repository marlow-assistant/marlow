# Onboarding v2: a shorter, structured first run

**Status:** wording pass merged 2026-10-18, harness
not built. The pass, ahead of the next user onboarding: new greeting; one
reminder, then the Schedule, then stop; reminder vs note explained; "things to
try" as its own early message; the briefing and week-ahead asked about, the
inbox check told not asked and shown as "Check-in" on both clients; USER.md and
the first approval explained; detours return to the step. Still open: the
harness below, Drive, dictation. Source: Taylor's first run on her
own instance, 2026-10-17, talked through with Mike afterwards. She is the first
user who neither built Marlow nor watched it being built, so this is the
first outside read of `identity/FIRST-RUN.md` and the greeting
(`server/app.py` `FIRST_RUN_GREETING`).

## What happened

1. Greeting: "I run on your own machine, reach you here…", then "what should I
   call you?"
2. Name, then a time-zone question: Marlow saw that her location didn't match
   the install's time zone and asked what to do about it.
3. After that it stopped. The greeting had promised "a few minutes" of setup,
   but no step-2 question came. Taylor had to ask "what's next for the setup?"
4. Marlow then looked through Gmail and Calendar, said what it could see, and
   asked what to stay on top of: "bills, appointments, a trip, a project or
   something recurring like your bins. Name one and I'll set it up now, either
   as a reminder or a note I keep watching."
5. Setup grew complicated fast: the morning briefing, inbox check and week
   ahead were presented as already set up, with no ask first.
6. An approval prompt came up. Taylor approved without knowing what it was.
   Later Marlow said "I'm saving what I've learned about you to USER.md, please
   approve it", which didn't explain it either.
7. About 20 minutes in, after she had already asked it to remember something
   and set reminders, Marlow closed with "things to try next": send a photo of
   a letter or a receipt, tell me something to remember, ask for a reminder.

## The feedback, by theme

### The script lost its place (the main finding)
- The time-zone anomaly derailed it. Asking about it was reasonable (Taylor:
  reminders depend on it), but it shouldn't come first, and afterwards Marlow
  forgot the rest of the setup.
- Today the whole of FIRST-RUN.md is one set of instructions handed to the
  agent once, then free conversation. Nothing holds it to the sequence, so the
  first detour ends onboarding. Mike: "We dump a bunch of instructions on
  Marlow and expect it to follow them."

### Explain before doing
- Before it touches her mail or calendar, say what Marlow can do: reminders,
  notes, lists, finding things in email and calendar.
- Explain the difference between a reminder and a note. The step-4 question
  used both words with no explanation.
- Explain approvals the first time one appears: what the prompt is, why
  Marlow asks, and what USER.md is. Today's line (FIRST-RUN step 7) assumes
  the user knows what USER.md is. Taylor only knew from Mike explaining it
  elsewhere.

### One thing at a time, and ask before setting up
- Taylor's shape: set up one reminder together, then take the user to the
  Schedule to see where it lives. Then stop. (She corrected herself on
  "one reminder and one note" down to one reminder.)
- Morning briefing, inbox check and week ahead: ask whether she wants them
  before setting them up. Fact check: install.sh seeds all three, and
  FIRST-RUN step 5 only tells the user about them and asks whether the times
  suit. To Taylor that read as "it set them up for me". The fix may be the
  wording, opt-in seeding, or both.
- The inbox check is different (Mike, 2026-10-17): don't ask about it at
  all. It is what keeps Marlow relevant through the day, and other assistants
  simply watch the inbox without asking. Asking makes an internal job look
  like a setting. It also needs a user-facing name: "email flash" is ours, and
  something like "check-in" says what the user gets. It stays the job's
  internal name (`cron/email-flash.md`); only what the user sees changes.

### "Things to try next" is misplaced
- It should be its own message and come earlier, not tacked onto the wrap-up.
- By then it was redundant: she had already done two of the three.
- "Send a photo of a letter or a receipt" came with no reason. Say why it's
  useful, or drop it.

### Greeting wording
- "I run on your own machine" means nothing to a non-technical user. Taylor:
  what machine? Nothing of hers was nearby. Her instance runs on Mike's box,
  so it isn't her machine either. Needs rewording for someone who doesn't know
  what a server is.

### Google Drive isn't reachable (a capability gap, not wording)
- Taylor connected Drive alongside Gmail and Calendar at claude.ai. Marlow
  doesn't use it: it could open a Drive link she pasted, but not through the
  connector she approved. Her expectation: if Gmail and Calendar just work, so
  should Drive.
- Probed 2026-10-17 as `taylor`, CLI 2.1.302 (`scripts/probe-drive-connector.sh`):
  `claude mcp list` and the session's init both show Drive **connected**, but
  it contributes **no tools**. Even `ToolSearch` for the exact name
  (`select:mcp__claude_ai_Google_Drive__list_recent_files`) finds nothing,
  while Gmail's lookup on the same run works. So it isn't the identity or the
  allowlist, not yet: the CLI gets an empty tool list from Drive. Not a general
  Drive failure either: a Claude Code desktop session on Mike's account has
  the Drive tools. Re-run as `marlow` (Mike's account, CLI 2.1.301), now with
  Google split into four connectors (Drive, Docs, Sheets, Slides): the same.
  Docs, Sheets and Slides contribute tools; Drive contributes none. So it is
  the Drive server or the CLI, not Taylor's grant and not our code. Re-check
  after a CLI update; nothing for us to build until Drive tools load.
- Once tools load: tell the identity about Drive, and pick which Drive read
  tools join the `deploy/marlow-settings.json` allowlist (none do today, so
  every Drive call would ask the phone).

### Dictation looked like send-only (both clients)
- The mic in the message bar fills the text field, but Taylor thought the
  blue send arrow was her only next step and didn't realize she could review
  and edit the text first. The two controls need to look like two different
  actions. Could also be part of a walkthrough. Check `web/` too.

## Direction (Mike and Taylor)
- More structured and somewhat shorter. Possibly rigid and scripted, "like a
  video game tutorial": you can't get past a step until you've done it.
- Smaller units: give Marlow one step at a time and have it report when that
  step is done, instead of the whole script up front. The server would hold
  the user's place in onboarding rather than the agent.
- Whatever is built needs iOS and `web/`.

## Harness design (proposal, Mike likes it; future task, 2026-10-17)

The server owns the step list and decides when a step is done by checking
the result. The model saying "I'm done" is the weaker version: the model is
what lost its place, so it shouldn't keep the place either.

Each step has three parts:

1. **A short step line added to every turn.** `server/turn_context.py`
   already adds a `[Context: …]` line to each turn. During onboarding the
   server would add one more, e.g. "Onboarding step 3 of 6: set up one
   reminder with them." Because the line comes back every turn, a detour like
   the time-zone question can't end onboarding: the next turn still names the
   step. This alone fixes the failure Taylor hit.
2. **A check the server makes after each turn.** The queue's post-turn work
   already commits the working tree and records fired one-offs, so the server
   can see each step's result itself:
   - name step: the user replied;
   - reminder step: a new entry in `cron/schedules.json`;
   - wrap-up: USER.md written.

   The next step starts only when the check passes. The model only declares
   "done" where nothing on disk shows it ("asked what they want to stay on
   top of"), and even there the user's reply is a better signal than the
   model's claim.
3. **Fixed text and app screens where no judgment is needed.** Today's
   greeting is already fixed text. Some steps should be too: "here's what I
   can do", what an approval is, "tap here to see your Schedule", and a
   pointer to the mic. They could be fixed server messages, or app overlays
   that wait for a tap. That is the video-game-tutorial feel, and it can't
   drift.

Consequences: `FIRST-RUN.md` becomes a list of server-owned steps, each with
its own instructions and check, instead of one long script the model reads
once. The server keeps a small "which step is this user on" record. Both
clients need the step screens.

Phasing: parts 1 and 2 first, server only, with no app changes. Part 3 later,
on both clients.

Related, already parked in `friends-alpha.md` → "Pairing papercuts": the
greeting wiping the big face, the device name, the QR opening the browser, the
Tailscale battery settings.
