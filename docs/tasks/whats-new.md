# Task — tell the user what changed in an update

**Status (2026-10-23): shipped on both clients (iOS and web), merged.** **Observed** on Mike's iPhone against his box on the branch: the
card on foreground, and at launch with a box-only test entry (after a fix:
the first build missed launch, see `presentWhatsNewIfDue`). **Never
observed:** the web card on a real Android phone (an Android user will
check), dark mode on either client. Raised by a
second user on the box: a new build arrived and nothing said "your Marlow is
updated, and this is what changed". For an assistant whose case rests on
privacy, updates should be transparent, and an update that changes what Marlow
can reach should never land silently.

## The policy: when an update gets an entry

The test is one question: **would they be surprised to find out, or act
differently if they knew?** If yes, it gets an entry, in plain words, in the
same PR as the change.

**Always tell** (the `category` says which; clients draw these in the accent):

| category | what it covers |
|---|---|
| `access` | Marlow can read, send or reach something new or different: connectors, tools, egress, the approval rules |
| `unattended` | a new or changed scheduled job, i.e. anything Marlow does without being asked |
| `data` | a new place Marlow stores things, a change in how long it keeps them, anything new leaving the box (location, notification content) |
| `personality` | an identity change that alters how Marlow behaves or what it remembers |
| `removed` | anything taken away, including a restriction lifted |

**Tell briefly:** `feature` for something new they can use; `fix` for a
problem they would have noticed ("voice mode no longer cuts off").

**Don't tell:** refactors, tests, docs, hardening that changes no behaviour,
fixes nobody would notice. Never bury an always-tell change in a `feature`
entry.

Write for the person, not a developer: what they will notice, and what they
can do about it. "Marlow can now read your Google Drive when you ask. It asks
first, the same way as for email" — not "Drive connector added to the
allowlist".

## The format: `changes.json` at the repo root

```json
{"id": 4, "date": "2026-10-24", "part": "server", "category": "access", "text": "…"}
```

- **`id`** goes up by one per entry and is never reused or renumbered.
  **5 is skipped:** it was a device test entry on the PR branch, shown on
  Mike's phone and then removed, so that phone has it marked seen.
  Clients remember which ids they have shown. Two branches that both take the
  next id conflict in the file on merge, which is the point: renumber one.
- **`part`** is which half the change lives in. `server` entries are served by
  `GET /v1/changes` from the box, so they appear only once the box runs that
  code. `app` entries are bundled into each client at build time (iOS through
  `project.yml`, web through an import), so a client-only install brings its
  own notes without a box deploy, and an app built from a newer `main` than
  its box never announces server behaviour the box isn't running yet. A change
  spanning both halves (most features) is `server`: the box half usually
  changes what Marlow can do, and an app not yet updated just lacks the button
  for a while, which is a delay, not a false claim.
- `tests/test_changes.py` parses the real file strictly, so a malformed entry
  fails the suite. On the box `server/changes.py` logs it and serves nothing
  rather than refusing to start.

## How the clients show it

- **The card.** On launch and on foreground (a box can update while the app is
  backgrounded), each client merges the two halves and shows the entries it
  has not shown before, in "Marlow was updated". **OK** closes it, under a hint that
  What's new can be turned off in Settings. (There was a "Don't show this
  again" button; people read it as hiding this one change, so it went,
  2026-10-25.) The card never
  sits over the approval card, another sheet, voice mode or a recording; it
  waits until they are out of the way. On iOS it is a case of ChatView's single
  sheet; on web a modal that Chat holds back.
- **Seen is per device and a set of ids**, not a high-water mark: the halves
  update at different times, so app entry 12 can be shown before the box
  deploys server entry 11. A device that has never shown the card (a fresh
  install, or the first build with this feature) gets the newest five and
  treats the rest as history.
- **Not before onboarding.** `GET /v1/changes` also says
  `onboarding_complete`: false while USER.md still has the starter's "Not yet
  known" (the marker first-run onboarding keys on; fails open if unreadable).
  Until it flips, the clients show nothing and count every entry as seen, as
  when muted, so finishing onboarding never opens on a backlog. A box older
  than the flag omits it and clients read that as complete. Someone who never
  finishes onboarding never gets the card; the list is still in Settings.
- **Settings → Updates**: the What's new list (everything this device knows of,
  newest first) and "Show after updates", the way back from muting. While
  muted, new entries count as seen, so turning it back on doesn't dump a
  backlog.

## Not built

- **Branch deploys** aren't flagged on the card. `/health` and Settings' Build
  rows already show the branch.
- **Muting covers everything**, always-tell categories included. A user who
  turns it off has said so; the list is still in Settings.
