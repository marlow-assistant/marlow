# Task — configurable model and thinking effort

**Status: DONE 2026-08-27. All three pieces shipped and merged.** Pieces 1 and 3
2026-08-19 — server-side defaults, shipped together (see "Why 1+3 are one PR and
2 is another"). Piece 2 2026-08-27 in two halves: server
(`docs/tasks/effort-toggle-server-half.md`) and iOS. Three probes run. Verified
end to end on the box the same day: a `high` turn from the phone logged
`effort_requested=high, effort=high`.

Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

`/security-review` was run on the server half — the one place client input
reaches the CLI arg list — and found nothing. The value is matched against a
closed tuple twice, in `app.py` and again in `invoke.py` at the argv boundary,
and both return **the matched tuple member rather than the request string**, so
what reaches argv is one of three literals by construction. The second check is
not redundant: `run_claude` is also reachable from cron and compaction, which
never pass through `_prepare_message`.

**Settled 2026-08-27 — the compaction rule is STRUCK.** "Compaction takes the
session's current level" is withdrawn; compaction runs at the configured
default, full stop, which is what the code already does. Measurement killed the
cheap way of implementing it and no reason survives to buy the expensive way —
see "Probe 3" below.

The problem: nothing in the stack configured either. Prod inherited whatever the
CLI defaulted to, so a CLI update could silently move Marlow onto a different
model or effort level and the first symptom would be latency or a bill.

## What was measured on the box, 2026-08-18

From the live attended session's `.jsonl` (70 assistant records):

- **Model: `claude-sonnet-5`, `service_tier: standard`,** on every record. Not
  Opus. This was inherited, not chosen.
- **Effort is low. Confirmed 2026-08-18 by `probe-effort-low-adaptive.sh`** —
  `low` is *adaptive*, spending zero thinking on most turns and hundreds when the
  model judges it needs to. Prod's nonzero thinking is exactly what low looks
  like. (A brief intermediate reading that prod must be on medium was wrong; it
  generalised from a single prompt. Mike caught it.) The CLI records
  `message.usage.output_tokens_details.thinking_tokens` per assistant message —
  a *direct* measurement. Max on any single message **757**; typical nonzero
  100–260; 5,863 total across the session.
- **Caveat: assistant records appear to be written twice.** Values come in exact
  pairs (`13 13`, `112 112`…) and there were 36 nonzero counts against 17 actual
  `thinking` content blocks. Halve the raw figures — ~18 thinking messages,
  ~2,900 tokens for the day. Anyone re-running this must not report the raw sum.
- **Thinking is not a cost lever here.** ~2,900 thinking tokens against
  **5.8M** cache-read tokens in the same session. Effort is a *latency* dial on
  this workload, not a spend dial. The savings work is `idle-compaction.md`.
- Thinking blocks store `signature` plus an **empty** `thinking` string, so
  reasoning content is never persisted to disk — only its volume. You can audit
  how much it thought but never what it thought.

Reproduce (structure only, no conversation text):
`jq -r 'select(.type=="assistant") | .message.usage.output_tokens_details.thinking_tokens // empty' <session>.jsonl`.
The trap: grepping the box for `THINKING` hits the session `.jsonl` files, but
that is *conversation text* (Mike and Claude discussing this topic), not
configuration. The `usage` field is the only real signal.

## Probe results — `scripts/probe-effort-flag.sh`, run on the box 2026-08-18

**Both unknowns closed. The flag works in print mode and the per-turn toggle is
safe.** One run each:

| `--effort` | thinking_tokens | output_tokens | duration_ms |
|---|---|---|---|
| low | **0** | 448 | 5,773 |
| medium | 211 | 899 | 10,969 |
| high | 414 | 923 | 13,016 |

- **`--effort` is honoured in `-p`.** Thinking and latency rise cleanly with the
  level. Low is ~2x faster than medium and ~2.3x faster than high on the same
  prompt.
- **`low` thought zero on this prompt** — briefly over-read as "low never
  thinks". It does; see the ladder below.

**Mid-session effort switch — cache impact, same run:**

| turn | effort | cache_read | cache_create |
|---|---|---|---|
| a | low (warming) | 24,732 | 1,056 |
| b | low (warm) | 25,788 | 524 |
| c | **high (switched)** | 18,492 | **8,356** |
| d | high (warm) | 26,848 | 1,342 |

**Not a cold read.** ~8K of the prefix moved from read to create for one turn and
recovered immediately; prefix totals are flat throughout. At cache-write vs
cache-read rates that is a fraction of a cent. **Verdict: the per-turn toggle
ships as designed.** Caveats: n=1, and turn c's re-cache is not cleanly
separable from a normal cache-block boundary rolling over (a and d also wrote
1–1.3K). It doesn't matter: the phone sends a *stable* value every turn, so the
cost is paid only when Mike moves the picker.

### Follow-up ladder — `probe-effort-low-adaptive.sh`, 2026-08-18

Thinking tokens, low vs medium, same prompts:

| prompt | low | medium |
|---|---|---|
| trivial ("capital of France") | 0 | 0 |
| puzzle (three switches) | 0 | **0** |
| hard (logistics design) | 39 | 1,473 |
| plan (arrange my week) | 0 | 205 |
| tradeoff (NAS vs cloud) | 0 | 98 |
| chitchat ("anything I should know today?") | **427** | 605 |

1. **`low` is adaptive, not a hard zero.** **Prod is on low.** Piece 1's pin is
   the no-op it was meant to be; pinning `low` changes nothing about how Marlow
   behaves today, it only stops the level being inherited.
2. **Effort is a nudge, not a dial — and run-to-run variance is large.** The
   puzzle prompt thought 211 tokens at medium in the first probe and **0** in this
   one. Don't build anything assuming a level produces a given amount of
   thinking, and don't treat a single future measurement as a regression.
3. **The chat-shaped prompts hold the real finding, and it isn't effort.**
   "Anything I should know about today?" was the *most* expensive prompt at low —
   427 thinking tokens, 1,634 output, **27 seconds** — because it reaches for
   calendar and mail, not because it thinks. On Mike's traffic **latency is
   dominated by tool use, and effort is noise next to it.** The ~2x spread from
   the first probe overstates what a chat default costs in practice; chase
   perceived slowness at tool round-trips first
   (`docs/tasks/turn-activity-streaming.md`).

### Probe 3 — `probe-effort-resume-inherit.sh`, 2026-08-27

Run to test Mike's proposed dodge: if `claude -p --resume` with **no**
`--effort` flag inherited the session's level, "compaction takes the session's
current level" would be a one-line conditional (omit the flag for
`source="compaction"`) instead of remembering each session's chosen level.

**It does not inherit.** The most useful thing this probe turned up: **the
session `.jsonl` stamps a top-level `effort` field on every invocation**, so the
level is readable off disk rather than inferred from token counts. For a session
built at `high` and then resumed three times with no flag:

```
['high', 'high', 'low', 'low', 'low', 'low', 'low', 'low']
```

A no-flag resume takes **the CLI's own default**, not the session's level. The
dodge is dead, and worse than neutral: omitting the flag would reintroduce the
silent inheritance piece 1 exists to prevent.

**The trap, which nearly produced the opposite answer.** Thinking tokens on the
follow-ups looked like textbook inheritance — the high-built session thought
1254 / 31 / 1218 against the low-built session's 22 / 13 / 15 on *identical*
prompts. That gap is real and **not effort**; it is history. The high-built
session's opening answer was 3192 output tokens against 971, so its follow-ups
resume a far richer conversation. Two sessions built at different levels are not
a controlled comparison for anything measured after the build. **Trust the
stamped `effort`; treat the token columns as corroboration only.**

**On `/compact` specifically.** It runs (14.7s and 35.8s, `is_error=False`) and
the effort flag *is* stamped on the compaction turn, so effort reaches it. But
the envelope reports `output=0 cache_read=0` — the summarization request's usage
is not exposed — so **whether the level changes the summary is unmeasured and
unlikely to be measured.** The 2.4x duration spread is not evidence: the two
sessions had different prefixes. An earlier run of the probe had two flaws: it
reused one follow-up prompt three times (runs 2–3 degenerated into
restatements), and it never printed `is_error`, so an ~85ms all-zero **error**
envelope read as "/compact ignores effort". Both fixed in the script.

### Anthropic's documented position on switching effort mid-conversation

Mike surfaced the upstream docs 2026-08-18. Effort is request-level and shapes
the rendered prompt, so changing it between requests **does not preserve cached
prefixes**; the docs advise picking a level at the start of a long session and
keeping it constant. That is *stronger* than the probe saw (~8K moved, not a full
cold read). Trust the documented behaviour over one small-session observation.

**It does not change the design** (Mike's call). The phone sends a stable value
every message, so the re-cache is paid only when the toggle is moved. It changes
how the toggle is *presented*: a "settle on a level" control, not a "think
harder about this one" button. Don't evolve it into a per-message button
without re-reading this section.

## The levers — first-class CLI flags, confirmed on 2.1.235

`--effort <level>` and `--model <model>` (alias or full name) are both
**per-invocation and session-scoped**, so per-source or per-turn differentiation
needs no re-architecting. Print-mode behaviour was settled by the probes above,
run in a **clean shell** (never nest `claude -p` inside a Claude Code session).

## Piece 1 — pin the defaults

Mike's call 2026-08-18: Sonnet on low is right for a personal assistant, medium
is plausible for trickier work, Opus is not needed, and high/xhigh/max just add
latency. Defaults: **`--model sonnet`, `--effort low`**, pinned explicitly even
though prod already runs Sonnet — the point is to stop inheriting, not to change
behaviour. The `sonnet` **alias**, not a full version string: pin the tier, track
the latest Sonnet ("pin the decision, not the version"). *(The shadow run later
found that an alias can resolve to an older model than expected; see
`model-shadow-run.md`. Here the tracking is the intent.)*

### Where the config lives, and why not the two obvious alternatives

`server/config.py` + a `MARLOW_*` env override: deploy-time configurable for free
(systemd `Environment=`, like `MARLOW_MEMORY_MODE`), and the only layer that can
later become per-user without moving.

- **Not `~/.claude/settings.json`.** It is user-global for the `marlow` account,
  so it would also change Mike's own interactive sessions when he SSHes in to
  reauth. And that file is the *permission policy*, held in lockstep with
  `server/boundaries.py` by `tests/test_marlow_settings.py`; performance tuning
  in a security file makes both harder to reason about.
- **Not the systemd unit alone.** The code can't see it, tests can't assert on
  it, nobody discovers it.

Two implementation traps. The flags must go in the `args` list **before** the
`args += ["--", prompt]` line: everything after `--` is positional, so a flag
added below is silently passed to the model as *text* — it looks like it worked
and does nothing. And log the chosen model/effort in the existing invoke log
line or telemetry, or the setting is unobservable from outside.

## Why 1+3 are one PR and 2 is another

Mike asked 2026-08-18 whether all three should be one PR. The split is 1+3 / 2.
1 and 3 are the same review question (right defaults, do they reach the
process?); piece 3 is a few lines keyed on `source`. Piece 2 stands alone because
it is cross-stack (Swift and Python review worse bundled), needs the local-only
iOS gate, and — the real reason — **is the only piece where client input reaches
the CLI arg list**, so it alone warrants `/security-review`. Piece 1's value
doesn't wait on piece 2: it stops prod silently inheriting a CLI default change
the moment it deploys.

## Piece 2 — user-configurable effort, from the phone (shape decided 2026-08-18)

**Mike's call:** an iOS UI toggle, not a server-side preference store. The phone
owns the setting and **sends the requested effort with every message**; the
server validates and applies it per invocation. That is the *per-turn* shape and
needs **no server-side persistence**, so `session_state.py` stays the two-fact
file its docstring promises. *Since 2026-10-16* the box also keeps a copy of the
chosen level (`/v1/preferences`, `server/preferences.py`) so every client shows
the same one (Mike); it only keeps clients in step, and the turn still runs on
what the message carries.

**Model is display-only.** Effort toggles; model does not: effort is cheap to
switch mid-session, a model switch pays a full cold prefix read. Mike confirmed
2026-08-18 that the Claude GUI warns about exactly this. Showing the model
without offering to change it is the honest version of that constraint.

Choices: the picker offers **low / medium / high** only (`xhigh` and `max` only
add latency here); the phone stores it in `@AppStorage`, default `low` matching
the piece 1 pin; it sends the field on **every** message, not just on change (a
stateless field is easier to reason about than a sticky one). Server side,
`effort` is **optional** on the request (an older client keeps working) and an
invalid value falls back to the default rather than erroring, so a client bug
degrades instead of breaking the conversation. A cron turn carries no client
effort and takes the piece-3 default.

**The header: a plan that was built and did not work.** The first plan moved the
icon + "Marlow" pair to `.navigationBarLeading` and appended the setting after the
text. **It did not render on device.** A leading item is allotted roughly the left
third of the bar (the centre stays reserved for a title even when none is set), so
icon + "Marlow" + status hit `lineLimit(1)` against zero width and the status
vanished silently. Nothing was wrong with the value; there was no room to draw it.
**What shipped** (Mike, on device, 2026-08-27): the status *is* the title —
`Sonnet/Low`, centred — and the leading item is the icon **alone**, a circle.
Built as a `.principal` `Text` rather than `.navigationTitle` so VoiceOver can
say "Running Sonnet at Low thinking effort" instead of "Sonnet/Low".
**The lesson:** no unit test could have caught this, and the iOS test count
didn't move when it was fixed (150 both ways). It was a SwiftUI render-time clip visible only on a
device. A nav-bar layout change gets *looked at*, not asserted.

### Compaction must follow the session's selected level — WITHDRAWN 2026-08-27

**This rule is struck. Compaction runs at `settings.effort`, full stop**, which
is what `_effort_for_source` already does. Do not reintroduce it without reading
Probe 3. The argument for it was only "no evidence medium summarizes better",
which argues for *not raising* compaction, not for tracking the session. What
killed the cheap version: a no-flag resume takes the CLI default, so "omit the
flag for compaction" would reintroduce silent inheritance. What's left is the
expensive version — the server remembering each session's effort — and Mike's
standing fallback ("cron and compaction both run at medium, full stop") already
said not to pay for it.

**The one thing genuinely lost:** a session running at high gets its compaction
summarized at low. Whether that matters is unmeasured and, per Probe 3, hard to
measure. If a summary is ever observed to drop something it should have kept,
that is the signal to revisit; this is the paragraph to re-read first.

### SECURITY NOTE — read this before wiring any client-supplied value to a flag

**A user-configurable effort means untrusted client input reaching the CLI's
argument list.** That has bitten this repo already: `session_hint` arrives
unvalidated from the phone, and passing it as two tokens (`["--resume", value]`)
let a value beginning with `-` be re-parsed as a flag in its own right — one
request field became arbitrary CLI flag injection. `--settings={...}` alone
re-injects the `ANTHROPIC_*` vars `_scrubbed_env()` strips and can define hooks,
i.e. command execution (confirmed by spike 2026-08-03). The fix was the
`--resume=<value>` single-token form, which binds the remainder as data.

So: **never interpolate the client-supplied effort string into the arg list.**
Match it against the closed set `low|medium|high` and pass the matched
**constant**, not the input. An allowlist here is the same class of hole closed,
not over-engineering.

## Piece 3 — cron turns run at medium (decided 2026-08-18)

**Mike's call: yes.** Scheduled work runs at **`--effort medium`**, chat at
`low`. The argument is latency: nobody is waiting on the 07:30 briefing, so the
one real cost of higher effort isn't paid. And scheduled turns are where quality
has a long half-life, while a chat reply that takes longer to say the same thing
is worse. The cost objection doesn't bite (~2,900 thinking tokens against 5.8M
cache-read tokens a day). This supersedes the earlier "only if Mike sees
consolidation writing thin memories" rule, which was about not differentiating
without evidence; a latency argument needs none.

### The rules, and why they are deliberately not uniform

| turn | effort | why |
|---|---|---|
| `consolidate` | medium (**high** since 2026-10-18, with `ideas` — see "Per-job effort") | Open-ended judgment, writes to `memory/`, longest half-life of anything Marlow produces |
| `morning`, `weekly-digest`, `week-ahead`, `alberta-weekly` | **high** since 2026-10-19 — see "Per-job effort" | The long reads Marlow writes; nobody waiting |
| other cron (`email-flash`, `physio-nudge`, schedules entries) | medium | Latency unperceived, quality persists |
| compaction | **the configured default** (was "the session's current level" — STRUCK 2026-08-27) | No evidence medium summarizes better; a no-flag resume takes the CLI default (Probe 3), so the cheap implementation doesn't exist |
| chat | the user's level | It is the user's choice |

**Why compaction differs from consolidate:** `consolidate` asks a genuinely
open question, and deliberation can change the *answer*. Compaction does bounded
selection, and more thinking there plausibly produces the same summary more
slowly. **Neither is measured.** The asymmetry is a reason to treat them
differently, not proof. Mike's standing fallback ("cron and compaction both run at medium,
full stop") was not what shipped for compaction: it runs at the configured
default (low), per the table and the WITHDRAWN section.

### The cost we are accepting: `consolidate` attaches to the day's session

Found in review of the piece 1+3 server defaults; it invalidated the "cron work
is detached" premise. `consolidate` is in `ATTACHED_SESSION_JOBS`, so it
**resumes Mike's attended phone session**, and running it at medium switches
effort mid-session and cold-reads the day's prefix.

**Accepted deliberately.** The prefix is bounded at ~110K tokens by idle
compaction's threshold — about **2% of a day's read volume** (5.8M measured) —
the job is once a day, the cost is Pro quota rather than dollars, and
`consolidate` is also in `RETIRING_JOBS`, so the session dies right after and
nothing downstream inherits the cold cache. The first instinct in review was to
make attached cron turns inherit the session's level. **Rejected:** machinery to
save 2%, coupling effort to `resume`/`session_id`, and it trades the quality of
the most consequential turn of the day for a rounding error.

**Watch item — it has already fired once.** The 110K bound exists *because* idle
compaction caps the session. Measured 2026-08-19: after an evening deploy the
watcher was blind overnight (its state is in-memory and only attended turns feed
it), nothing compacted, and this turn cold-wrote **578K** tokens — 5x the bound.
Read "~2% of a day's reads" as a normal-day figure, not a guarantee. Mike's call
was to accept it rather than persist watcher state; reasoning in
`idle-compaction.md`, "Judgment calls made while building".

## Per-job effort, and why not Opus (2026-10-18)

A cron turn's effort is resolved in `app._cron_effort` at enqueue, in order:

1. `ideas` and `consolidate` always run `high` (`LOCKED_EFFORT_JOBS`); entries
   cannot change it (cache reasoning below).
2. Otherwise the schedules entry's optional `"effort"` key (`low`/`medium`/`high`;
   refused on `notify`). A `--entry` dispatch is looked up by entry id only, so a
   model-named entry called `consolidate` never inherits that job's default. A
   job dispatch is looked up by job name; if two entries run the same job, the
   higher effort wins.
3. Otherwise the product default: `morning` and `week-ahead` are `high`
   (`HIGH_EFFORT_JOBS`).
4. Otherwise `cron_effort` (medium).

Only the server sets a cron turn's effort, and `_effort_for_source` still
matches it against `_ALLOWED_EFFORTS`.

**Per-entry effort (2026-10-19, Mike):** users should never have to know effort
exists. Marlow sets `"effort": "high"` itself when it writes a research or
synthesis job nobody waits on, leaves it off reminders and quick checks, and
explains it only if asked (`identity/CLAUDE.md`, "Scheduled jobs"). No client UI.
This replaced naming jobs in server code: Mike's own `weekly-digest` and
`alberta-weekly` were in the code set and moved to their entries
(`deploy/migrations/0006-entry-effort.py`). The store is agent-writable, and the
worst an entry can do is make a turn slower: the value is one of three, the daily
firing cap bounds the turns, and the locked jobs are out of its reach.
`morning` and `week-ahead` take an entry's override (unlike the locked pair)
because nothing shares their cache and "make the morning brief quicker" is a fair
ask.

**Added 2026-10-19 (Mike):** `morning`, `weekly-digest`, `week-ahead` and
`alberta-weekly` — the synthesis jobs whose reply is the product, with nobody
waiting — run `high`. The cost is a cache re-write when the next chat turn at
medium follows one: once a day after `morning`, once a week after the others.
`email-flash` stays medium: four a day, mostly a triage that ends `[silent]`;
`high` is the lever if it starts missing mail that mattered.

Why `ideas` and `consolidate` first: both write things that outlive the day
(memory, the ideas store) and run after midnight with nobody waiting. Thinking
is a rounding error against a day's cache reads. They share one level because
`ideas` (00:30) and `consolidate` (01:00) sit in the same session inside the
prompt cache's hour, and an effort change between them re-writes part of a cache
the second could have read.

**Opus was the other option, and was not taken without evidence.** Measured on
Mike's box over 14 days (API-equivalent prices; Opus 5.5 is 2x Sonnet 5.5 on
input, output and cache writes, the same on cache reads): scheduled jobs cost
more than chat (~$28 against ~$16), because they mostly start cold. Opus for
`consolidate` alone adds about 20% to the total, mostly by losing the warm cache
`ideas` leaves; Opus for every job, about 55%. Latency does not matter for
either. The one Sonnet memory error on record (a wrong stop location,
`model-shadow-run.md` day 1) came from a chat reply that `consolidate` copied,
which neither a model nor an effort change would fix. If Opus is revisited, the
shadow run (`model-shadow-run.md`) is the way to measure it, and `ideas` and
`consolidate` should move together.

**No model or effort toggle on the clients.** The model is a per-box operator
setting; a user who wonders "am I suddenly using more?" sees it in the Usage
panel, and each turn's model and effort are shown in its activity row (since
2026-10-18).

## Reference

`server/invoke.py` (arg builder, the `--` rule, the `--resume=` flag-injection
history); `server/config.py` (`MARLOW_*` override convention);
`docs/tasks/idle-compaction.md` (why effort is not the cost lever; the
compaction-quality question); `docs/tasks/marlow-settings-permissions-policy.md`
(why `settings.json` is the wrong home).
