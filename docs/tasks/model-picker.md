# Task — the user picks the model (Sonnet or Opus), box-wide

**Status: in progress, 2026-11-12.** Server, web and iOS on one branch.

## The decision (Mike, 2026-11-12)

One model per box, chosen by the user in Settings, used by **every** turn:
chat, scheduled jobs, compaction. Effort still varies: chat takes the user's
"Think time", scheduled jobs keep the server's ladder (`model-and-effort-
config.md`, "Per-job effort"), which does not depend on the model.

- **Why not a different model for chat and jobs.** It was the first proposal
  (a per-entry `"model"` key, Opus for the synthesis jobs). Mike's objection:
  the UI would name a model the back end sometimes doesn't use. It was also
  the expensive shape: `consolidate` resumes the day's chat session, so a
  model different from chat's rewrites that whole conversation every night.
  With one model, the only cold rewrite is the reply after the user changes
  the setting.
- **Why no Haiku.** The shadow run (`model-shadow-run.md`) had it get facts
  wrong on the jobs Marlow cares about, and in the 2026-11-12 benchmark
  snapshot Haiku is not faster to first token than Sonnet at any level.
- **Why every combination is allowed.** Opus at low effort starts answering
  about as fast as Sonnet at medium (5.1 s against 5.0 s) and scores higher on
  the snapshot's index; Mike's own plan is Opus everywhere with Quick chat.
- **Supersedes** "Model is display-only" and "No model or effort toggle on the
  clients" in `model-and-effort-config.md`. The cost argument there (a switch
  pays a cold prefix) still holds and is why this is a "settle on one" setting,
  never a per-message control.

## What the cache measurement said (Mike's box, 31 days to 2026-11-12)

Counts from `usage.jsonl` and the session files, nothing else:

- **The prompt cache lives one hour.** Every cache write on the box is in the
  CLI's 1-hour bucket, none in the 5-minute one.
- Of 387 chat turns, 340 came within an hour of the previous invocation and
  wrote 2–5K tokens each against ~200K read. The 47 after a longer idle wrote
  ~76K each: three-quarters of chat's cache writes, since a write costs ~40x a
  read. Median first output was 1.9 s either way, so cold costs quota, not
  felt speed.
- Scheduled jobs wrote 14.9M against chat's 4.8M: they mostly start cold.
- A model switch rewrites one conversation, the size of one of those 47 idle
  turns (~$0.60–1.60 at API prices on 100–200K). About 1.5 happen a day anyway.

**A keepalive ping was costed and not built.** A read that keeps 200K warm
costs ~$0.02 and saves ~$0.30 only when a real turn follows inside the hour;
pinging through the day nets ~$0.15 a day. A ping is also a real turn (it
appends to the session unless forked, which is untested), holds the work
queue, and may count against Pro's limits.

## Shape

- `preferences.py` keeps `model` beside `effort`, one of `MODELS`
  (`sonnet`, `opus`); unset means the operator's `MARLOW_MODEL`.
- **The server reads it at the argv boundary, the client never sends it per
  message.** `invoke` matches the stored value against `MODELS` and passes the
  matched constant, like effort (the flag-injection note in
  `model-and-effort-config.md`). The store is server-private, so the only
  writer is `POST /v1/preferences`, which also takes only those two values.
- `/health` reports the model in force, so the header ("Opus/Low") and the
  activity rows follow without a client change; `/v1/preferences` reports the
  choice (null until picked).
- Clients: the "Intelligence" section becomes **Model** (Sonnet / Opus) and
  **Think time** (Quick / Balanced / Thorough, still `low`/`medium`/`high` on
  the wire). The footer says what each costs: Opus uses a plan's limits about
  twice as fast, and longer think time makes Marlow slower to respond.
