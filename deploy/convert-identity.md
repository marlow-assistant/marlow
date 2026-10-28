# Converting a box to the PREFERENCES.md layout

For the box owner's own Claude (or the owner) to follow, once per box. A box
seeded before `PREFERENCES.md` existed keeps a `CLAUDE.md` that mixes two
things: rules that come with Marlow's software, and the owner's own
preferences, some added by Marlow since the seed. From this release:

- **`CLAUDE.md` belongs to the software.** `update.sh` replaces it on every
  update, and Marlow can read it but not edit it.
- **`PREFERENCES.md` belongs to the owner.** `CLAUDE.md` imports it on its last
  line. It is never replaced, and Marlow's edits to it ask the owner's phone.

`update.sh` only takes over `CLAUDE.md` once `PREFERENCES.md` exists. Until then
it prints "no PREFERENCES.md: this box predates the split, leaving CLAUDE.md
alone". This guide writes that file. Get it right and nothing personal is lost;
anything left out of it is dropped silently when `CLAUDE.md` is replaced.

Paths: the working dir is `~marlow/marlow-identity`, the software is
`~marlow/marlow-server`. Run everything as `marlow` (`sudo -u marlow ...`) so the
files stay owned by the user Marlow runs as.

## 1. Read, change nothing

```bash
W=~marlow/marlow-identity; S=~marlow/marlow-server
sudo -u marlow cat $W/CLAUDE.md                     # the box's current rules
sudo -u marlow cat $S/identity/CLAUDE.md            # what replaces them
sudo -u marlow cat $S/identity/PREFERENCES.md       # the starter preferences
sudo -u marlow git -C $W log --oneline -- CLAUDE.md # every change since the seed
```

If the history starts with a `seed:` commit from the starter template, this
shows everything Marlow or the owner added since:

```bash
sudo -u marlow git -C $W diff "$(sudo -u marlow git -C $W rev-list --max-parents=0 HEAD | tail -1)" HEAD -- CLAUDE.md
```

## 2. Sort every rule in the current CLAUDE.md

The test: **if it would change when Marlow's server changes, it is the
software's. If it would change when the owner does, it is theirs.**

- **The software's, drop it:** anything `identity/CLAUDE.md` already says, even
  in other words. That includes `[silent]`, the schedules store, the shell
  command rules, approvals, git being automatic, and what the app shows.
  Software rules that are missing from the new file are not the owner's to
  keep. Mention them to the owner, but leave them out of `PREFERENCES.md`.
- **The owner's, keep it:** how they like replies (length, tone, format), how
  proactive to be, their own jobs and what to do with them (for example "when
  I report a workout, log it in memory/fitness-log.md"), their tools and
  languages, and where they usually live.
- **Not sure:** ask the owner. Don't guess.

If the box has a `cron/email-flash.md`, `update.sh` now replaces it with the
software's version. That version decides who and what matters from `USER.md`
and memory, so a person or topic named in the old one should be a fact there
(who they are, what the project is), not a rule for the job. Other job
prompts, like `cron/morning.md`, stay the owner's and need nothing.

Facts about the person (family, job, home) belong in `USER.md`, not here. If
one turns up in `CLAUDE.md`, check `USER.md` already has it, and ask the owner
before adding it there.

## 3. Check first run won't trigger

The new `CLAUDE.md` starts a getting-to-know-you conversation while `USER.md`
still says "Not yet known" under "Everything else":

```bash
sudo -u marlow grep -A3 "Everything else" ~marlow/marlow-identity/USER.md
```

If it does, tell the owner. Either that conversation is welcome (it was added
after their box was set up), or they replace that line with a sentence or two
about themselves first.

## 4. Draft PREFERENCES.md and show it

Start from `identity/PREFERENCES.md`. Keep its headings, swap its generic
wording for the owner's own where theirs is more specific, and add a section
for anything else from step 2. Use the owner's name, not "them".

**Show the owner the whole draft, and list what you dropped as the software's.
Write nothing until they say yes.**

## 5. Install

Write the agreed draft to `~marlow/marlow-identity/PREFERENCES.md` as `marlow`
(for example to a temp file you own, then
`sudo -u marlow cp /tmp/PREFERENCES.md ~marlow/marlow-identity/`). Then:

```bash
sudo ~marlow/marlow-server/deploy/update.sh
```

It should print `CLAUDE.md installed` and `FIRST-RUN.md installed`. Run it again
and both say `unchanged`.

## 6. Check it from the phone

1. Ask Marlow to add "End every reply with 🦜" to PREFERENCES.md. The phone
   asks; approve it. The next reply should end with 🦜. Then ask it to take
   the line out again.
2. Ask Marlow to add a rule to its CLAUDE.md. It should say it can't and offer
   PREFERENCES.md instead.

## Undo

The working dir is under git. Before Marlow's next turn commits the new
files, the old `CLAUDE.md` is still `HEAD`'s. Afterwards it is one commit back:

```bash
sudo -u marlow git -C ~marlow/marlow-identity log --oneline -3 -- CLAUDE.md
sudo -u marlow git -C ~marlow/marlow-identity checkout <commit> -- CLAUDE.md
sudo -u marlow rm ~marlow/marlow-identity/PREFERENCES.md
```

With `PREFERENCES.md` gone, the next `update.sh` leaves `CLAUDE.md` alone again.
