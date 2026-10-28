# One conversation — phone and cron share a session

**Status: MERGED 2026-09-26.** Mike's call, same day.

## The symptom

Every cron job except `consolidate` ran in a fresh `claude -p` session, and
only phone turns continued the chat. The phone rendered both in one thread, all
"from Marlow", which hid the split. Two failure shapes, both of which read to
Mike as "Marlow is dumb":

- a job blind to what he had said an hour earlier (and then excusing itself
  for not having read some file);
- chat caught flat-footed when he replied to a message a job had just sent,
  because the chat session never saw it.

## What changed

- `server/session_state.py` owns **the conversation**: the session id of the
  last successful conversation turn, phone or cron. Persisted as
  `conversation_session_id` (reads the old `last_attended_session_id` key once,
  so the live conversation carries over the deploy).
- Phone messages and every cron job and schedule entry enqueue with
  `in_conversation=True`; `_drain` looks the conversation up **when the turn
  runs**, not at enqueue. That one choice replaces the phone's first-turn send
  gate and the server's `_remap_pending_sessions`: a turn queued behind one
  that starts or replaces the session simply resumes wherever that turn left it.
- The phone no longer holds or sends a session id (`session_hint` removed from
  `MessageRequest`; old builds still send it and pydantic drops it). This also
  takes the only client-supplied string off `--resume`.
- `consolidate` is unchanged in shape: it binds the session at enqueue,
  refuses a fresh fallback, and retires it. Retiring now clears the
  conversation, so the **08:00 morning brief starts each day's session** and the
  phone's first message resumes that. If consolidation failed, the day runs on
  in yesterday's conversation — the lesser evil.
- Schedule entries (model-authored) run in the conversation too, but still can
  never take consolidate's retire path (`CronMessageBody`'s job/entry split).

## The risk accepted

Separation had one real safety argument: untrusted content a job reads
unattended (email, in the morning brief) died with that job's session. Now it
stays in the conversation where Mike later approves tool calls. Weighed and
accepted: chat already reads the same mail whenever he asks it to, and the
permission hook is per-call, not per-conversation, either way.

## Parked — build only on a felt symptom

- **Fork per job.** `claude -p --resume <id> --fork-session` would give a job
  the conversation's context while keeping its tool noise (email bodies, a
  dozen tool steps) out of the chat, with only the final message injected back
  via `server/turn_context.py`. Only worth it if clutter is shown to hurt —
  slower turns or compaction firing noticeably more often after jobs. Needs a
  probe first: unverified that `--fork-session` works with `-p` on the box's
  CLI. Idle compaction runs when Mike is away, so he does not wait for it.
- **`notify` entries are still invisible to the conversation.** A pre-written
  reminder (`schedules.json` `notify`) never goes through the model, so if Mike
  answers one, Marlow has not seen it. None exist on the box today. Fix if it
  bites: queue the text and prepend it to the next conversation turn.
- **Idle compaction still keys off the last *phone* turn** — its session and
  its size (`idle_compaction.note_attended_turn`), not the conversation. A job's
  growth is seen at the next phone turn, and if a job self-heals the
  conversation into a new session, the watcher can compact the abandoned one.
  The fix is feeding session+size from every conversation turn while keeping
  the idle clock phone-only — subtle in that module, so parked until it shows.
- **Two dead-session edges** (review 2026-09-25, both rare): a self-healed turn
  that still ends ERROR leaves the conversation on the dead id, so the next
  turn pays one extra failed invocation; and `consolidate` binds its session at
  enqueue, so a session swapped by a turn queued just ahead makes that night's
  review ERROR instead of running. Both were possible before this change.
- **Interleaving.** A job firing mid-exchange lands between a question and its
  follow-up. Mike: not worried.
