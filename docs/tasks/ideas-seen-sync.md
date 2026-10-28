# Task — sync "ideas seen" across clients

**Status:** not started (2026-10-16). Mike, while the face and Thinking level
went to the box (`server/preferences.py`, branch `marlow-faces`): opening Ideas
on the phone should clear the lightbulb's dot on web too, and the other way
round. Not urgent. **Both clients** (root `CLAUDE.md`).

## What happens today

The dot means "an idea is new since the panel was last opened"
(`ideas-panel.md`). Each client keeps its own "last opened":

- **iOS:** `IdeasStore.lastOpenedKey` (`"ideasPanelLastOpened"`) in
  `UserDefaults`, a `Date`.
- **Web:** `KV_IDEAS_OPENED` (`"ideasPanelOpened"`) in IndexedDB, a local
  day string (`useIdeas.ts`, `lib/ideas.ts` `showDot`).

So an idea read on one client stays dotted on the other.

## Shape, when it's picked up

The same pattern as face and Thinking, so most of it exists:

- **Server:** add `ideas_opened` to `/v1/preferences`. Unlike those two it is
  a time, not a choice, so the box should keep the later of the stored and the
  sent value rather than the last write, or a client with a slow clock or a
  stale pending send moves it backwards.
- **Clients:** an `Item` in iOS `PreferenceSync` and web `lib/preferenceSync.ts`
  (pending until confirmed, never overwritten while pending, pulled on
  foreground). Both clients need one format first: the two stores differ
  (a `Date` against a day string), and `showDot` compares against an idea's
  `added` day.

## Open questions

- Day or instant? Web's day granularity means a second idea added later the
  same day doesn't dot. Worth settling before both clients share one value.
- Does dismissing the spotlight card count as "seen"? Today it doesn't.
