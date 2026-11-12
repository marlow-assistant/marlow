# Backlog analysis: what to build next

**Last full review: 2026-11-12** (Opus 5.5, builder session). Mike's lens for
it: favour "make Marlow more useful" over polish and infrastructure.

This file holds the opinion. The backlog itself is factual and lives
elsewhere, so this file can go stale without anything breaking: when it's older
than a couple of weeks, or the work it ranks has moved, redo it.

## Where the backlog lives

Read all four for a review:

1. **Mike's wishlist on the box**, the capture surface:
   `ssh marlow@marlow 'cat ~/marlow-identity/memory/marlow-features.md'`.
2. **`feature-backlog.md`**: wishlist items that have no task doc yet,
   mirrored here so a builder session can see them.
3. **`marlow-v2-START.md` "Open work"**: the in-flight and follow-up items.
4. **`docs/tasks/*.md` Status lines**: the truth about any one item.

## How to redo this review

- Read the four sources. Check what a Status line claims against the box
  (`git -C ~/marlow-server log -1`) before trusting it.
- Rank by how much more useful Marlow gets for someone using it, then by
  cost. Mark anything blocked upstream or waiting on a device check.
- Replace the ranked list below, update the date at the top, and add one line
  to the review log at the bottom. Don't keep old rankings: git has them.
- When an item gets picked up, it gets a task doc (and leaves
  `feature-backlog.md`), and its box wishlist entry gets a `→` pointer.

## Ranked list (2026-11-12)

### Small jobs that pay off quickly

1. **Opus for research jobs** (`feature-backlog.md`). Add a `"model"` setting
   to each schedules entry, the same way `"effort"` works, which Marlow sets
   itself on research jobs nobody waits on. The 2026-10-18 measurement
   (`model-and-effort-config.md`) puts Opus on every job at about 55% more;
   on those jobs alone it's much less. `ideas` and `consolidate` must move
   together because they share a cache. No switch in chat: every change there
   pays a cold prefix.
2. **Stop showing to-do ids in replies** (`feature-backlog.md`). The fix
   exists only on Mike's box. One line in `identity/CLAUDE.md` "To-dos"
   delivers it to every box, plus a `changes.json` entry.
3. **Notification flood after a computer wakes** (`feature-backlog.md`). In
   `web/src/sw.ts`, drop stale pushes or ones already read elsewhere, and
   merge a backlog into one notification. Small, and felt every time.

### Bigger features that add capability

4. **Garmin connection** (`feature-backlog.md`). The biggest new ability on
   the list: activities, sleep and health data arrive on their own instead of
   via screenshots. The gates come first: the MCP servers are unofficial and
   take the user's own Garmin credentials, so Mike reads the code; the
   operator installs it; the credential goes in the env-secrets path.
5. **Approval card names the Drive doc** (`tool-approval-ux.md` piece 2).
   Mike would see which document he's approving, not an opaque ID. The trap
   recorded there: the title must not come from the model.
6. **File edits as red/green diffs** in the transparency sheet
   (`feature-backlog.md`), both clients.
7. **"Watch this and tell me" tasks** (`feature-backlog.md`). Notify-only,
   on the schedules store, as the safe first step. Acting on Marlow's own
   judgment needs a guardrails design before anything is built.
8. **Share and files, and the image viewer** (`share-and-files.md`,
   `image-viewer.md`). Already specced for implementor agents.

### Measure before building

9. **Snappiness and protocol overhead** (`feature-backlog.md`,
   `reply-length-and-perceived-speed.md`). The 2026-11-04 data says lookups
   cause the slowness, not transport; audit one turn's traffic before
   changing anything.
10. **Read-aloud on a real phone** (`read-aloud.md`). Built on both clients,
    never heard. Checking, not building.

### Parked or lower priority

- Maps in replies, the quota-timed memory "dreaming" pass, and the anonymised
  usage report (`feature-backlog.md`).
- Google Doc to markdown: blocked upstream until Drive gives `claude -p`
  tools.
- `side-chats.md`: parked by Mike while the ideas panel is one tap away.
- A model-agnostic runtime: breaks the CLI-only hard constraint, so it's
  Mike's to reopen.

## Review log

- 2026-11-12: first review. Created `feature-backlog.md` from the box
  wishlist; ranked as above.
