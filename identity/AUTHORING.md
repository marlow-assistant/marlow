# AUTHORING.md — why the starter CLAUDE.md says what it says

<!-- NOT SEEDED. seed-identity.sh copies CLAUDE.md, not this file. This is the
     reasoning behind each section of the starter identity/CLAUDE.md, kept
     here so the file the assistant reads every turn can stay short. Change
     CLAUDE.md, then check the matching section below still holds.

     The section bodies below were the template's placeholder text until the
     starter identity replaced them (2026-09-29); they are the "why", and the
     starter is the "what".

     KEEP IT SHORT. Every line here is read on every turn, and an instruction
     that does not change what the assistant DOES is pure cost. Two shapes to
     avoid, both found in the live file on 2026-08-08:

       - Restating something the harness already supplies. The per-turn prompt
         opens with a [Context: ...] line carrying Mike's current timezone and
         local time; a standing "we are in Mountain Time" block competes with it
         and loses the moment he travels.
       - Copying a harness fact that lives in code or config. An enumerated cron
         schedule here is a second copy that drifts silently -- the live one was
         wrong about every time it listed, and named a delivery channel from v1.

     Write what the assistant should DO. Let it look things up. -->

## Two files: CLAUDE.md is the software's, PREFERENCES.md is theirs
<Split 2026-10-05 (docs/tasks/box-upgrades.md). Until then every rule below was
seeded once into a CLAUDE.md the user owned, and no update reached it. A
release whose fix was a sentence here -- `[silent]`, the first-run pointer --
never arrived on a box seeded before it.

So CLAUDE.md is now product code written in English. update.sh replaces it on
every deploy (on a box that has PREFERENCES.md), and the agent can't edit it
(boundaries.PRODUCT_OWNED_PATHS). PREFERENCES.md holds how this person likes
things done, is seeded once, and edits to it ask the phone like SOUL.md.

Which side a section goes on: if it changes when the SERVER changes (the
context line, memory format, the schedule store, the shell policy, approval
mechanics), it is CLAUDE.md. If it changes when the USER does (tone, length,
how proactive to be), it is PREFERENCES.md.

The import is the last line of CLAUDE.md, never the first line of
PREFERENCES.md: the software owns the file that carries it, so nothing the
agent edits can drop it. Last, so the user's text follows the rules it may
override. Same directory, because an import from outside the project may wait
on an approval a headless run can't give; a sibling import was confirmed under
`claude -p` from a clean shell (2026-10-05).

Nothing in CLAUDE.md or FIRST-RUN.md may carry a {{placeholder}}: update.sh
copies them verbatim, with no fill pass.>

## Startup
<What should the assistant read/check at the start of each session?>

## First run
<Added 2026-10-02 (docs/tasks/friends-alpha.md item 9). In the first dry run
nothing asked who the user was; Mike gave his name unprompted, and the
assistant edited USER.md behind an approval. Three pieces, kept in step by
tests/test_deploy.py::FIRST_RUN_MARKER:

  - The server opens a fresh box's conversation with a canned greeting on the
    first pairing (server/app.py FIRST_RUN_GREETING). It is a system turn, so
    the model never saw it, and FIRST-RUN.md has to tell it the greeting went
    out. It fires when the new device is the only one paired and the user has
    never typed a message. Each half alone misfires: a lost token file looks
    like no devices, and retention can empty a quiet box's transcript. It
    doesn't require an empty conversation, because the morning briefing can
    reply before anyone pairs. FIRST-RUN.md allows for a skipped greeting.
    The greeting asks ONE thing, the name. The first version also asked what
    they wanted help with; the user answered the name only and the model
    never came back to it (first VM run, 2026-10-02).
  - CLAUDE.md carries only a three-line pointer, keyed on USER.md still saying
    "Not yet known". The script itself lives in FIRST-RUN.md because CLAUDE.md
    is read every turn forever, and the script is dead weight once onboarding
    ends. The pointer exempts scheduled runs: a morning briefing on day one
    must not turn into a questionnaire nobody is awake for.
  - Onboarding ends when the assistant replaces the marker paragraph. That is
    its one USER.md write, one phone approval. Replace it even if little was
    learned, or the assistant stays in first run.

FIRST-RUN.md is built around one question since 2026-10-17: "what do you want
to stay on top of?" The user should leave the first conversation with real
reminders already running and know where to see them (the Schedule panel),
not with a tour of features. The calendar and inbox reads happen BEFORE the
question, so it can carry real examples; a bare "what would you like help
with?" got vague answers. Recurring `cron` + `notify` reminders were always
supported by the store but never documented in CLAUDE.md, so the assistant
didn't know it could set "bins out every Monday" without a job prompt. The
first user this was shaped for wanted to be organized more than to chat.
First run also writes one or two standing ideas, because the nightly `ideas`
job hasn't run yet on day one and an empty lightbulb reads as broken.
`ideas/ideas.json` is a named writable file, so this costs no prompt.

The version before was a guided tour that SHOWED: it read their real calendar
and inbox, did one useful thing, and offered a real reminder, each step ending
in the assistant's own offer. The first version was a checklist of questions, and
on the VM it answered the name and then sat at "What do you need?" (Mike: a
new user who isn't shown what it can do doesn't come back). It also wrote
USER.md mid-conversation; the script now says not before the last step.

Second VM run (2026-10-02) got through every step, then spent 172s on the last
turn. The USER.md save went out as four Edits, four prompts, to someone who
had never seen one; the first timed out and the model reported "you declined
the name change", so the name stayed wrong. Hence: one Write, announced in a
streamed line before the prompt appears, and the CLAUDE.md line that a "Not
approved" is not a refusal. The hook's denial string is deliberately
ambiguous across deny, timeout and unreachable (hooks/pretooluse.py), so the
fix belongs in the identity, not the hook.

Calendar list_events and Gmail search_threads are the demo because both are in
boundaries.VETTED_PATHLESS_TOOLS, so they run without a prompt. search_threads
is the list view; opening a message (get_thread) prompts by design, so the
script says not to. Say what is prompt-free in FIRST-RUN.md; an assistant that
expects a prompt announces one.

THE NAME LIVES ONLY IN USER.md. SOUL, CLAUDE and the cron prompts say "they"
and "the user", and so does the server's per-turn context block. Before this,
the install name was filled into five files and a systemd drop-in, so
correcting it cost about five phone approvals and could never reach the
drop-in. Don't put a {{USER_NAME}} back anywhere else;
test_seeded_name_lives_only_in_user_md fails if you do.>

## Identity
See SOUL.md.

## Write rules
<When does the assistant persist something to memory/? Note that the harness
commits automatically after any file change -- see docs/tasks/
marlow-working-dir-git-policy.md -- so this section is about WHAT to write, not
about running git.

Say HOW to write, not just when. Found on the box 2026-09-04: a memory file had
grown nine sibling bullets under one heading -- "Revised on the day", "Locked
plan", "Superseded plan (kept for reference)", and one whose entire content was
a note explaining which other bullets had been deleted. Every individual edit
was correct; each one read the file and removed the lines it made stale. The
defect was emergent across all nine, and answering "what is true now" had come
to mean reading the whole block and applying supersession rules stated in prose.

Four rules close it, and they are cheap because git carries the history already
-- the harness commits every turn and the assistant can read `git log`/`show`:

  - Rewrite, do not stratify: edit the lines a change makes wrong, rather than
    adding a dated "Revised" bullet beside them. Never leave a note saying what
    was removed; that is a hand-written diff of a file that is already versioned.
  - Current state first, history below it, under a heading that says it is not
    current.
  - Do not date a section heading. "sketch, drafted Aug 29" reads as an artifact
    to annotate; the same content under "Itinerary" reads as state to rewrite.
    The frame is what drives the behaviour -- naming a moment is the trigger.
  - Move the substance, move the frontmatter `description:`. Under
    MARLOW_MEMORY_MODE=index_only that line is the whole per-turn view of the
    file (server/identity.py), so a description that has drifted from its body
    hides its own file.

The tendency behind this is worth stating plainly for anyone tempted to relax
these: adding is always locally correct and pruning is only ever globally
correct, so a turn-scoped agent will never prune on its own. See
docs/tasks/memory-file-shape.md.

Tell the assistant its files are read by a person, not only by itself. Since
docs/tasks/ux-lane.md §3 the app shows SOUL, USER, CLAUDE.md, every top-level
memory file, the schedules and each job's instructions, read-only, with the
`description:` as each memory file's subtitle. Without that line the assistant
writes memory as private scratch; with it, the rules above have a second reason
to hold.>

## Shell commands
<The harness vets some Bash locally and prompts the phone for the rest, and the
assistant cannot see which is which -- so say what SHAPE is free. Everything
below is auto-approved as of 2026-09-06 (server/bash_policy.py); anything else
costs a prompt, and on a cron run a prompt is an auto-DENY.

  - Reading NAMED files, and ONLY with these verbs -- `cat`, `grep`, `head`,
    `tail`, `nl`, `sed`. This is the whole list, not examples: a verb that is
    not on it prompts even when it only reads. The rule is that every target
    must be a file that EXISTS, inside the working dir, and is not a secret. A
    directory target is not, so `grep -r pattern .` and `cat memory/` still
    prompt -- name the files.
  - `sed` is free in ONE shape: a line-range print, `sed -n '210,280p' file.md`.
    That is `head` with a start line. Any other script prompts (`-i`, `s///`,
    `d`, `$`), because sed's script can write and execute, not just read.
  - `2>/dev/null`, `2>&1`, `;`, `&&`, `||`, and a pipe into a bare `head`,
    `tail`, `wc`, `sort`, `uniq`, `cut`, `cat`, or `grep`.
  - A pipe into `grep` may carry a PATTERN but never a filename:
    `cat notes.md | grep -i pattern` is free, `... | grep pattern other.md` is
    not (that is a second file, so name it with a plain `grep` instead). `-r`
    in a pipe is never free -- with no filename it searches the directory, not
    the stream.
  - `ls`, `wc`, `date`, `echo`, and read-only `git`.

Not free, and no phrasing makes them free: `$(...)` and backticks, `>` and `>>`,
globs (`*.md`), `find`, `python3`, `crontab`, `sudo`, and any path outside the
working dir.

KEEP THIS HONEST WHEN THE VETTER CHANGES. This section exists because the
opposite was tried and failed: the live file and two cron prompts spent a month
telling the assistant to prefer the `Read` tool over `cat`, naming `cat` as
denied outright, and it ran `cat` anyway a week later
(docs/tasks/marlow-harness-affordances.md). Routing the model away from the
shell by asking does not work, so the harness meets it where it is: this
section's job is to describe what is cheap ACCURATELY, not to talk it out of
the shell. A stale line here is worse than no line -- it teaches a detour
around something that now works.

Say that `Grep` and `Glob` ARE available and are the right tool for "which file
mentions X" and "what is in this directory" -- they are the one thing `Read`
cannot substitute for, and every unrecoverable cron denial in the transcripts
was asking exactly that. They were absent from the CLI's tool list until
--tools named them (2026-09-06); an identity written before that told the
assistant it could not reach them, which was true then and is wrong now.>

## Response style
<Tone, length, formatting preferences. Lives in PREFERENCES.md, with the
proactivity bullets from "Acting in the world": both are about this person,
not the software. Only the approval mechanics stayed in CLAUDE.md, under
"Approvals".>

## Scheduled jobs
<Three things, all behavioural: how replies reach the user; that a job runs IN the
one conversation (since 2026-09-25 -- it sees the chat, and the chat has seen
what the job sent, so neither should apologise for missing context the
transcript holds; `notify` reminders are the exception, never seen by the
model); and that the assistant OWNS the schedule -- `cron/schedules.json` in the working dir is the live source,
edited directly, re-rendered by the server inside the same turn.

State plainly that `crontab` and any scheduling skill are the WRONG mechanism.
An identity that merely omits the store is not neutral: found on the box
2026-08-30, the day the feature deployed, where the live file still carried the
pre-feature "cron changes are Mike's to apply" and "job timing is his to change,
not yours". Asked for a reminder, the assistant obeyed -- reached for the Claude
Code `schedule` skill, then `sleep 300`. Both correct given its rules. The
capability shipped and nothing told it.

Show one `cron` entry and one `at`/`notify` entry as the format. NOT the schedule
itself, and not times -- a cron job arrives as an ordinary prompt, so the prompt
is the instruction.

Name the `[silent]` reply (2026-10-02). Until it existed, every cron reply
reached the phone, so a periodic check that found nothing still buzzed it with
"nothing to report". That capped the inbox check at twice a day. The server
drops a cron reply that is exactly the token, or empty. The identity is the
only thing that tells the assistant the token exists. Without that line it
keeps sending "nothing high-priority", which is correct by its rules.

The one exception to "not times": `email-flash` is tied to `morning`, three
hours apart, the last one in the evening. Its search window is relative and it skips what
the briefing already flagged, so its prompt doesn't depend on the clock. Its
schedule does, though, and the user moves the briefing in conversation
("make it 9"). The assistant edits one entry. Only a line here makes it move
the other one too.

Say that a one-off is `at`, and that `at` takes any payload -- `notify`,
`prompt`, or `job` -- so a one-off that needs a full prompt file is `at` plus
`job`, not a dated `cron` expression. That distinction is the one worth spending
a line on: a `cron` field has no year, so `"0 9 9 9 *"` fires every September 9th
forever, and "I will delete it after it fires" is a promise no turn-scoped
assistant keeps (same tendency as the Write rules above).

Name the `effort` key (2026-10-19), and say the assistant sets it unasked.
The user should never have to learn the word: a weekly digest should simply
come out considered, and a reminder should stay quick. Until this line,
`high` came from job names hard-coded in the server, and a job the assistant
wrote for the user could not get it at all. "Don't bring it up" is there
because offering a thinking-level choice reads as wizardry.

Do NOT restate the store's schema beyond that. The server validates it, says
exactly what is wrong in the per-turn context block when it refuses, and a
second copy here goes stale -- this paragraph said `at`+`job` was invalid for
about an hour on 2026-09-06, which was true when written and wrong by the time
it deployed.

Three things about time that the assistant will otherwise get wrong, all added
2026-08-31 after the cron timezone fix:

  - `timezone` is where MIKE is, never the server's zone. The harness converts
    to box-local when it renders; an assistant that "helpfully" pre-converts
    subtracts the offset twice.
  - The harness rewrites a one-off `at` with an offset (`...19:05-0230`) to pin
    the moment against a later zone change. Say that it is expected, so the
    assistant does not edit it back and undo the pin.
    Also say to Read the file again before the next edit: the rewrite makes
    the CLI's Edit fail as "modified since read", and on the second VM run
    the assistant told the user "the server reformatted the file" mid-turn.
  - When the per-turn context block says the store's zone disagrees with Mike's,
    OFFER to update it; never change `timezone` silently. The harness speaks up
    only when the two actually disagree, so the sentence always has a correction
    behind it -- but a silently retimed morning scan is hard to notice and harder
    to attribute later, and a phone can report a zone in passing. Worth telling
    the assistant that changing `timezone` moves recurring entries (the point)
    and cannot move pinned one-offs (also the point).>
