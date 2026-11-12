# Backlog analysis: what to build next

**Last full review: 2026-11-12** (Opus 5.5, builder session). Mike's lens for
it: favour "make Marlow more useful" over polish and infrastructure.

This file holds the opinion. The backlog itself is factual and lives
elsewhere, so this file can go stale without anything breaking: when it's older
than a couple of weeks, or the work it ranks has moved, redo it.

## Where the backlog lives

1. **`backlog.md`**: the single index of open work, one line per task doc,
   plus sketches of ideas with no task doc yet.
2. **Mike's wishlist on the box**, where new ideas are captured:
   `ssh marlow@marlow 'cat ~/marlow-identity/memory/marlow-features.md'`.
   Anything there with neither a task doc nor a sketch in `backlog.md` gets
   added to `backlog.md` during the review.
3. **Each task doc's Status line**: the truth about any one item.

## How to redo this review

- Read the sources. Check what a Status line claims against the box
  (`git -C ~/marlow-server log -1`) before trusting it.
- Rank by how much more useful Marlow gets for someone using it, then by
  cost. Mark anything blocked upstream or waiting on a device check. Fix any
  `backlog.md` line its task doc contradicts.
- Replace the ranked list below, update the date at the top, and add one line
  to the review log at the bottom. Don't keep old rankings: git has them.
- When a sketch gets picked up, it gets a task doc and becomes a one-line
  entry in `backlog.md`, and its box wishlist entry gets a `→` pointer.

## Ranked list (2026-11-12)

### Small jobs that pay off quickly

1. **Model picker** (`model-picker.md`), *shipped 2026-11-12*. Became
   a box-wide Sonnet/Opus choice in Settings instead of a per-job model: one
   model for every turn keeps the UI honest and avoids rewriting the day's
   cache when `consolidate` resumes chat.
2. **Stop showing to-do ids in replies** (`backlog.md`). The fix
   exists only on Mike's box. One line in `identity/CLAUDE.md` "To-dos"
   delivers it to every box, plus a `changes.json` entry.
3. **Notification flood after a computer wakes**, *done 2026-11-12*. Replies
   share one tag and a backlog folds into one counted notification; the
   server has no per-device read state, so a push that arrives late is quiet.

### Bigger features that add capability

4. **Garmin connection** (`backlog.md`). The biggest new ability on
   the list: activities, sleep and health data arrive on their own instead of
   via screenshots. The gates come first: the MCP servers are unofficial and
   take the user's own Garmin credentials, so Mike reads the code; the
   operator installs it; the credential goes in the env-secrets path.
5. **Approval card names the Drive doc** (`tool-approval-ux.md` piece 2).
   Mike would see which document he's approving, not an opaque ID. The trap
   recorded there: the title must not come from the model.
6. **File edits as red/green diffs** in the transparency sheet
   (`backlog.md`), both clients.
7. **"Watch this and tell me" tasks** (`backlog.md`). Notify-only,
   on the schedules store, as the safe first step. Acting on Marlow's own
   judgment needs a guardrails design before anything is built.
8. **Share and files, and the image viewer** (`share-and-files.md`,
   `image-viewer.md`). Already specced for implementor agents.

### Measure before building

9. **Snappiness and protocol overhead** (`backlog.md`,
   `reply-length-and-perceived-speed.md`). The 2026-11-04 data says lookups
   cause the slowness, not transport; audit one turn's traffic before
   changing anything.
10. **Read-aloud on a real phone** (`read-aloud.md`). Built on both clients,
    never heard. Checking, not building.

### Parked or lower priority

- Maps in replies, the quota-timed memory "dreaming" pass, and the anonymised
  usage report (`backlog.md`).
- Google Doc to markdown: blocked upstream until Drive gives `claude -p`
  tools.
- `side-chats.md`: parked by Mike while the ideas panel is one tap away.
- A model-agnostic runtime: breaks the CLI-only hard constraint, so it's
  Mike's to reopen.

## Review log

- 2026-11-12: first review. Created `backlog.md` from the task docs and the
  box wishlist; ranked as above.
