# Model shadow run — a second model on the same cron turns, side by side

**Status: ENDED 2026-10-17, code removed.** Taylor went Pro on Sonnet
(`marlow-for-taylor.md`), so the question it answered no longer arises. The code
lived on an unmerged branch, `model-shadow-run`, and is not in this repo: the
pieces were `server/shadow.py`, `invoke.run_shadow` with `ShadowSpec`, and
`WorkQueue._run_shadow`. The box's shadow config and scratch copies are gone
(checked 2026-10-26). What follows is the record.
It ran on the box from 2026-10-16 09:00, deployed from that branch, with
Haiku 5.5 at high effort shadowing the Sonnet 5.5 cron jobs, to decide whether
a cheaper Marlow (Taylor's, on an API key) was viable. Server-only, no UI on
either client: results were read by an Opus builder session. Probes P1–P4
passed for Haiku 5.5; `/code-review high` found 9 (8 fixed, 1 measured and
left) and `/security-review` found nothing at the bar.

Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

## Why

Taylor wants her own Marlow. On the API, Mike's last 14 days of `usage.jsonl`
priced at about $3.30/month on Haiku 5.5 versus about $66 on Sonnet 5.5 (flat
under-100K rate; Mike's session regularly crosses 100K, which pushes Haiku to
about $7–8). The cost was fine. **Whether Haiku can do the job was the open
question**: the silent-vs-speak call, the briefing judgment, memory discipline
and multi-step tool use are what small models get wrong first.

Options considered, in order of fidelity:

- **Offline API replay from the transcripts.** Rejected. The session `.jsonl`
  files keep every message and tool result, but not the system prompt (the
  identity rides on `--append-system-prompt`), Claude Code's own prompt or the
  tool definitions. A replay would judge Haiku in a reconstructed harness, and
  it starts every step from *Sonnet's* history, which hides Haiku's own
  compounding errors.
- **Haiku mode** (`MARLOW_MODEL=haiku`). Needs no code, but Mike pays for it in
  a worse assistant for the trial, and there's no per-input comparison.
- **Shadow run.** The real harness, the same inputs and live reads, with
  Sonnet's real output alongside. Compounding tool errors are visible. The cost
  is real code in `invoke.py` and extra plan quota.

**Cron only.** Chat can only be shadowed one turn at a time: after the first
reply that differs, Mike's next message answers Sonnet, not Haiku.

## How it worked

- **Cron is not self-contained.** Since 2026-09-25 every job and schedules
  entry runs in the conversation, so the 12:00 email check resumes the chat
  session. The shadow therefore had to **fork the real session at the point the
  real turn started**: copy the session `.jsonl` and its sidecar directory
  before the real turn appended to it, snapshot the working tree (no `.git`),
  and reuse the real turn's exact composed prompt and identity string rather
  than rebuilding them (a rebuild risks a different string, and calling
  `get_identity_context` for a second session id could register freeze state
  against the real conversation).
- **Run straight after the real turn**, inside the same drain step, still
  holding the queue slot: `email-flash` reads "new since last check" from a live
  inbox, and hours of drift would make the inputs differ. A chat enqueue
  preempts a running shadow (logged `preempted`); a queued cron job does not.
- **Confinement:** two extra bwrap binds (scratch working tree over
  `~/marlow-identity`, scratch projects dir over the real one), so every path
  in the identity and `boundaries.py` stays correct and the fork's `.jsonl`
  never lands among the real sessions. It failed closed if bwrap was off.
  The projects dir is bound on *every* run, fresh sessions included, not only
  when forking.
- **External writes: the existing unattended deny, nothing new** (Mike,
  2026-10-16). The shadow ran as `source="shadow"`, which `active_turn` treats as
  unattended, so any tool needing approval (Gmail `create_draft`, Calendar
  `create_event`, the Claude Docs connector's `update`) was refused and
  recorded. Tools stayed listed, so Haiku saw the same surface as Sonnet. Two
  alternatives dropped: a shadow-only hook layer (new code in the repo's most
  security-sensitive file, guarding a failure that would also break real cron;
  the guard is a test instead) and `--disallowedTools` (changes what Haiku sees
  and hides attempts).
- **WebFetch stayed allowed, as in real cron.** Haiku got the same rope as
  Sonnet. The exposure is a model reading stranger-written inbox text that can
  fetch a URL unprompted (START, "Facts that bite" §9); Mike accepted it for a
  short test, noting the shadow doubled how often it happens. Revisit if a trial
  ran past seven days.
- **Jobs chosen by a new `Turn.job`, not by `label`**, so a schedules `entry`
  Marlow named "email-flash" was never shadowed (`/code-review` finding).
- **Results to a log, nothing else.** One JSON line per run in
  `~/.marlow/shadow/shadow-runs.jsonl` (`0600`), content in each run directory.
  The shadow touched none of what the real path does after a turn (transcript,
  push, `session_state`, schedules sync, identity commit, activity log,
  promotion tallies). Telemetry recorded `source=shadow` rows without
  advancing the gap clock; the Usage panel under-counts Marlow's share
  slightly during a trial. Accepted.
- **A capture refuses a working tree containing any symlink** (a copied link
  would still point at the real file).
- **Cancelling any invocation now kills the CLI**, not just a shadow. Before, a
  cancelled turn left `claude -p` running.
- **Storage:** no cap; sessions ran 2–3.6MB a day (measured 2026-10-16), so 7
  runs a day kept 21 days is under ~2GB against 176GB free.
- **The model had to be a full id (`claude-haiku-5-5`), never the `haiku`
  alias.** See the first probe run below.

## Probes (2026-10-16, run from a clean shell on the box)

Per CLAUDE.md, no `claude -p` from a builder session. P1: a cross-model
`--resume --fork-session` works on a real Sonnet 5.5 session with thinking
blocks, gives a new session id, leaves the source byte-identical, and shows
Haiku in `modelUsage` (doubles as **P4**: Haiku 5.5 is available on this plan).
P2: with the projects dir bind-mounted from scratch, the CLI finds the copied
session and writes the fork there. P3: under the real confinement prefix plus
the work-tree bind, an `Edit` lands in scratch, the real file is untouched and
the secret tmpfs still hides what it hid.

**First run (CLI 2.1.284), against the live conversation (123 Sonnet 5.5
messages, 35 thinking blocks, no sidecar dir, which answers P2):** the
mechanics all held. Every invocation the box had logged (1,027 of 1,027) was
`confined=True`. **But the model was Haiku
4.5, not 5.5.** The script asked for `haiku`, the alias resolved to
`claude-haiku-4-5-20251001`, and its check passed because it matched "haiku"
anywhere in the name. Mike caught it. The script now defaults to the full id and
checks the exact model.

**Re-run, full id:** `claude-haiku-5-5` runs on this plan at 2.1.284 without an
upgrade (P4: yes). But the script's "newest session" pick found the first run's
own P3 session, because P3 had bound only the working tree and its fresh
session landed among the real sessions (deleted by hand). Hence the rule above:
bind the projects dir on every run.

**Third run (05:57): all probes pass for Haiku 5.5**, each claim checked
against the raw envelopes and the box, not the script's PASS lines. The
fork's `usage` equals that model's `modelUsage` entry, the Sonnet thinking
blocks caused no error, the real projects dir had no file modified, and
`~/.marlow/data` stayed hidden (7 entries outside, 0 inside). **One fork costs
about 55–69K tokens of history re-read cold on Haiku**, plus about 1K out at
`high` for a one-line answer.

**After the CLI upgrade to 2.1.301:** all pass again. The `haiku` alias now
resolves to `claude-haiku-5-5`, but the full id stays the rule, because an alias
moves with CLI releases. Two things the envelopes showed:

- **A resumed session's `modelUsage` carries the source's earlier totals.** The
  `claude-sonnet-5-5` entry was byte-identical in both forks ($4.10, 5.2M cache
  reads), so it is history, not a Sonnet call. Top-level `usage` holds only the
  new run.
- **The history check's question was ambiguous.** "The most recent message the
  user wrote" skips the cron prompts for a human reader. The forks quoted a chat
  message and the compaction summary, both genuinely in the session. It showed
  the history arrived, not that the forks matched.

## Evaluation

`scripts/shadow-pairs.py` laid each pair out for an Opus builder session to
judge, on Mike's plan with no API spend. The rubric: speak vs silent, whether
every claim traces to a tool result (fabrication a severe warning sign, judged
against Sonnet's own rate), coverage of must-surface items, memory-write
discipline, tool behaviour, rule adherence, cost signal. **`email-flash` was the
one to watch** (Mike): 4 times a day, the purest test of the silent-vs-speak
call. The verdict was a judgement call across three outcomes: clearly not smart
enough, sometimes (name which jobs and failure types), or obviously smart
enough. Planned length was 7 days, about 49 pairs; the trial ended after day 1
(see Status). Findings below are described by shape, not content, because they
came from Mike's real inbox and calendar.

### Early pairs (2026-10-16)

- **09:00 email-flash:** both searched the last 4h and replied `[silent]`.
  Sonnet also ran a route weather check for the day's drive; Haiku didn't.
- **12:00 email-flash:** both flagged the one item, a calendar invite from a
  known contact. Haiku added a caveat to check the invite really came from them,
  since it didn't recognise the address. Memory holds no address for that
  contact at all, so the line is literally true and misleading: invented caution
  about a real invite.
- **Effort replay of that 12:00 turn (Mike's question: is High trying too
  hard?).** Same identity (recovered from the CLI 2.1.301 transcript, 10,571
  chars, hash matched), same prompt, the 12:00 tree snapshot, through
  `run_shadow`. The inbox search was live, and a new logistics email had arrived
  since 12:00.

  | run | new email | the invite | "is it really them" caveat |
  |---|---|---|---|
  | live, High | (not there yet) | flagged | **yes** |
  | Medium #1 | flagged | **missed** | no |
  | Medium #2 | flagged | flagged | no |
  | High #3 | flagged | flagged | **yes** |

  The caveat appeared on 2 of 2 High runs and 0 of 2 Medium runs, which leans
  toward Mike's hunch. But Medium dropped a family item once, and four samples
  settle nothing. Decision (Mike): **keep High and gather more pairs** before
  changing effort. To replay a run, recover the identity from the fork
  transcript's `attachment.systemPrompt` block that starts `# SOUL`, and the
  prompt from its last user prompt row.

### Day 1 (2026-10-16 09:00 to 2026-10-17 06:30): 8 pairs

**Provisional read: "sometimes".** On the routine calls Haiku matches Sonnet.
Where it reasoned beyond the list view, it got specifics wrong.

**Health: clean.** All 8 runs came back `ok` with `ran_requested_model: true`
and no `denied_tools`, in 10–51 s. The real tree was clean, no canary anywhere,
only the two real sessions changed, no capture failures or preemptions, and no
chat turn waited behind a shadow. Five flashes rather than four because the
phone crossed a timezone line at 12:25 and the schedules follow the device; the
scheduler working as designed. The run dirs totalled 30 MB.

**Quota** can't be checked from the box (plan readings are held in memory only).
Day-1 tokens from `usage.jsonl`:

| | output | cache write | cache read |
|---|---|---|---|
| Sonnet, the 8 real cron turns | 5.6K | 436K | 912K |
| Haiku, the 8 shadows | 29.2K | 663K | 1,155K |

Haiku at `high` writes about 5x the output, mostly thinking (6.7K on the 16:00
flash, 8.6K on morning), and caches about 1.5x as much because every fork
starts cold. Mike's `/usage` at about 07:30 MDT Oct 17 read 2% for the week, but
the window had reset at 04:59 that morning, so day 1 couldn't be seen. One shadow
plus Marlow's morning therefore cost at most 2% of a week, and `/usage` put 8% on
Gmail and 7% on weather over 24 h, nothing shadow-specific. The plan
was: under about 5% a day keep every job; past that, drop `ideas` first, then
`morning`; keep `email-flash` and `consolidate`.

**The connector drop came back, on a cron turn** (now its own task,
`connector-drops.md`). The real 16:00 `email-flash` started without the
claude.ai connectors (a `deferred_tools_delta` at 22:00:02Z removed 44 tools,
all 30 Gmail and 9 Calendar among them). Sonnet followed its prompt and replied
`[silent]` without searching, so Mike got no 16:00 check. Connectors were back
by the 19:00 turn. The same drop hit `consolidate` (re-added 5 s later in the
same turn) and a chat turn at 06:45 on the 16th. The Haiku shadow 6 s after the
16:00 flash had Gmail, so that pair compares Haiku against a Sonnet that
couldn't look and is left out of the speak-vs-silent tally.

**Per pair:**

| pair | Sonnet (real) | Haiku (shadow) | read |
|---|---|---|---|
| 09:00 flash | silent, plus a route weather check | silent | same call |
| 12:00 flash | the invite | same, plus the false caveat | see above |
| 13:00 flash | a logistics email | same item | same; Haiku added a detail the email doesn't support, harmless |
| 16:00 flash | silent (no Gmail) | 2 items | not a fair pair; detail below |
| 19:00 flash | silent | silent | same |
| ideas | no change | 3 ideas added | both within the rules |
| consolidate | aged the travel log | same shape | Haiku is more accurate |
| morning | full brief | full brief | equivalent; small slips on each side |

Speak vs silent: **4 of 4 comparable flashes match**, with the same items
flagged.

**16:00, Haiku alone. Right instinct, wrong facts.** Haiku ran the second pass
and found something nobody else raised that day or the next morning: a calendar
commitment the following week that clashed with the trip. But three specifics
were wrong:

- **The time.** It read the calendar's `14:30:00-04:00` as 2:30pm. The calendar
  is set to an eastern timezone, so it was two hours earlier in Mike's local time.
- **The route.** It routed from the wrong starting city and said the event was
  "reachable if you don't lose time". Memory had the trip a day's drive short
  of that, so the verdict was wrong.
- **The people.** It got a relationship between two people in memory wrong and
  built its suggestion on it.

It also stated an inference from a truncated email snippet as fact.

**Morning.** Both briefs usable and close. Haiku misread the route tool's "Day 1"
as the trip's first day, which was days earlier, and stated a dated memory note
("draft still unsent") as current fact without checking. Sonnet broke a rule:
`travel.md` says a particular topic gets "no reminders", and its brief reminded
anyway. Neither brief raised the clash, because both read only today's calendar.

**Consolidate.** Both aged the Oct 16 leg into the trip log. The location
differs, and **Sonnet is wrong**: it put the night's stop past the next town,
when the device fix was about 50 km short of it. The mistake started in Sonnet's
own 21:15 chat reply and is in the real `travel.md`. Haiku placed it correctly,
and also put a harmless tool note into memory.

**Ideas.** Sonnet read both files and changed nothing, which the prompt calls a
normal night. Haiku added one `now` idea and two `standing` ones, each sourced
to a memory file, valid JSON, inside the caps, nothing previously declined. Both
legitimate; neither retired a stale `now` idea from a stop the trip was long past.

**Fabrication against Sonnet's rate, day 1.** Haiku made misleading claims in 2
pairs (the 12:00 caveat; the 16:00 time, route verdict and people). Sonnet made
one, the stop location, which reached memory, plus one rule slip. Haiku's errors
cluster where it *derives* a fact from a tool result: timezone offsets, route
start points, family relationships. If that held, the answer leaned toward
"Haiku for the list-view flash and consolidate, Sonnet for anything that
synthesises", or a prompt fix ("give calendar times in the user's timezone;
route from where memory says they'll be").

## Effort

The shadow ran Haiku at **`high`** against Sonnet's real `medium`
(`settings.cron_effort`), to show Haiku in its best light; nobody waits on a cron
job. What the published material said (read 2026-10-16):

- Anthropic's prompting guide recommends `high` for longer agent tasks and
  strict instruction following, and warns that at `low`, in long agent prompts,
  Haiku 5.5 is more likely to skip a search, stop early or skip a check: the
  failure an inbox scan can't afford.
- No head-to-head of Haiku 5.5 `high` against Sonnet 5.5 at any effort turned
  up; "Haiku high is close to Sonnet low" is unconfirmed. Artificial Analysis's
  index puts Haiku 5.5 at 29/34/38/41/43 from `low` to `max`, with about 8x more
  output tokens at `max` than at `medium`.
- Sonnet 5.5 leads on every published benchmark, widest on Terminal-Bench 4.0
  (70.6% vs 39.2%), narrowest on OSWorld 2.1 (83.9% vs 72.4%). Those are coding
  and computer-use tasks, not inbox judgement, which is why the trial existed.

## Decided (Mike, 2026-10-16; don't re-open)

1. **External writes:** the existing unattended deny plus a test, with no
   shadow-only hook layer and no `--disallowedTools`. This replaced the
   hook-layer recommendation in the first draft.
2. **WebFetch allowed** in the shadow, as in real cron. Mike accepted the
   exposure for a short test.
3. **Job set:** the template jobs, `morning`, `email-flash`, `consolidate` and
   `ideas`, which are the ones Taylor's Marlow would run. Mike's personal jobs
   and schedules `entry` prompts are excluded.
4. **Haiku runs at `high` effort**, against Sonnet's real `medium`.
5. **The verdict is a judgement call** across the three outcomes, with
   fabrication weighed against Sonnet's own rate.

## Not done

- **A per-source model setting.** The per-job verdict was to feed one, but it
  doesn't exist: `settings.model` is global, and only effort is per-source.
- **Chat shadowing.** Same mechanics would work one turn at a time while Mike is
  idle. Later, if the cron verdict had been promising.
- **The API-key question for Taylor** (do the claude.ai connectors survive a raw
  key, and does `--strict-mcp-config` become usable without account
  connectors). A separate two-minute probe.
- **Haiku mode on Mike's Marlow.** The cheap follow-up if the verdict was mixed
  and a felt test was needed.
