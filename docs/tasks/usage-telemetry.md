# Task — token/cache usage telemetry

**Status: Phase 1 MERGED** (2026-08-01) and recording.
`server/telemetry.py` writes one JSON line per invocation to
`$MARLOW_LOG_DIR/usage.jsonl`; `InvocationResult.usage` carries the counters
`invoke.py` used to drop. **Phase 2 (a standing analysis reader) is deliberately
not built** — two useful reads were done with a throwaway ~30-line script over
`json.loads`, which may simply be the right permanent answer. On by default,
`MARLOW_TELEMETRY_ENABLED=0` disables. The file is at
**`~/.marlow/logs/usage.jsonl`**, i.e. the *log* dir, not `~/.marlow/data/`
where a reader might first look. `RotatingFileHandler`, 10MB × 5 — at
~440 bytes/line nothing will roll for years, so history is safe.

Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

Requested 2026-07-25: *"after some days of usage we can look at how we're doing
on token and cache usage and tweak after the fact, with real data."*

## Why this exists

Every cache decision on the table at the time
(`docs/tasks/cache-friendly-identity-injection.md`,
`docs/tasks/memory-index-split.md`, how to space the morning crons) was being
made from a clean-shell spike on synthetic turns (`spikes/cache-cadence.sh`,
gate1-results §8b). That pins the mechanism but can't say what Marlow's real day
looks like. The stated design target (2026-07-25): **optimize for the reasonable
case, accept the idle miss** — morning crons close enough to share one cached
block, daytime 1–2 minute gaps still hitting, a 3-hour idle going cold and
accepted.

## What phase 1 records

Token counts (`cache_creation_input_tokens`, `cache_read_input_tokens`,
`input_tokens`, `output_tokens`), `source`/`session_id`/`resume`/`turn_id`,
`gap_seconds` (idle time since the *end* of the previous invocation; `null` on
the first invocation after a restart, never `0`), the identity block's size + a
short hash (so a cold-start miss can be told apart from a self-inflicted miss
caused by the block changing), and wall-clock duration. Every exit path records,
including `InvokeError` (a `finally` in `invoke.py`), so a failed turn's gap
isn't absorbed into the next turn's. Never logs prompt or result text — numbers
and hashes only: user content never goes in git history or logs. Later additions:
`num_turns`, `queue_wait_seconds`, `thinking_tokens`.

**Phase 2, if ever built:** a read-only script aggregating hit rate by source, the
gap distribution, tokens created vs read per day, and block-change vs expiry
misses. Deliberately not an endpoint or a dashboard. The log format is the
commitment; the analysis is cheap once there is something to analyze.

## What the first 32 turns say (read 2026-08-10, 2026-08-01 → 08-09)

Directional only. Confirmed recording on the box this day.

**1. The identity block is no longer the thing to optimise.** `index_only` cut
`identity_chars` 65,023 → 7,833 on 08-09 (see the second read for what that was
really worth). Total cost across the 32 turns was **~4.87M tokens**, the
overwhelming majority **`cache_read`** — individual turns read 206K, 264K, 344K,
355K, which exceed the 200K context window. That confirms the standing
interpretation: `cache_read` is the **sum across the invocation's internal
agentic loop**, so a long block costs *size × loop depth*. With the block small,
**loop depth is the lever**, and nothing then recorded it.

**2. Cron turns never resume.** Every `source:"cron"` row had `resume:false` —
the mechanical fact behind `nightly-consolidation.md`'s parked job.

**3. The gap distribution is nothing like the design target.** Gaps of 12s, 38s,
106s, 284s, 424s — then 7,128s, 37,769s, 45,197s, 53,435s. Real use is
**bimodal**: a burst, then hours of nothing. The idle case is the *common* one by
turn count, so any tuning that trades cold-start cost for warm-path savings loses.

**4. Health clean:** zero errors and zero permission denials across all 32.

**5. The volume is the finding: 3.5 turns/day.** Not enough dogfooding to surface
subtle problems. It became load-bearing in the 2026-08-10 priority call: the main
argument that `voice-mode.md`'s dictation half is **core capability rather than
sugar**, since voice changes *when* Marlow is reachable (van, ski, travel,
driving). **Re-read the volume before concluding anything from a low-signal log.**

## Second read — 57 turns (read 2026-08-12, 2026-08-01 → 08-12)

18 of the 25 new turns were on **08-11 alone**. The extra volume overturns the
first read's headline.

**1. Correction to "the identity diet was a 10x win". It was ~2x on minimal
turns, invisible in the median.** The first read's ~52K → ~5K cache-creation
rested on **one row** (a tiny turn, output=91). With 13 cold starts on each side:

| | cold-start `create`, median | minimal-work cold start (output <150) |
|---|---|---|
| identity ~64.5K chars | 45,434 (n=14) | ~36.8K, very stable |
| identity ~8.9K chars | 40,241 (n=13) | 15,765 (08-11T05:31) |

The **floor** dropped by roughly the size of the block removed (~16K tokens) —
real, and matches the mechanism. The **median** barely moved, because
`cache_creation` also accumulates as the agentic loop extends the cached prefix
with tool results. Cold-start create tracks **how much work the turn did** far
more tightly than identity size (worst row: create=119,311, output=8,606). This
reinforces that **loop depth is the lever**; the identity diet bought a smaller,
one-off win than we told ourselves, and little is left in `identity/` to cut. The
remaining prefix is the CLI's own system prompt, tool schemas and `CLAUDE.md`
(`docs/tasks/marlow-harness-affordances.md`).

**2. Caching works and the gap curve is textbook.** 81.8% of input tokens are
cache reads (7.83M read vs 1.73M created, 9.56M in / 72K out):

| gap since previous turn | turns | create/turn | read % |
|---|---|---|---|
| <5m | 21 | 13.6K | 89.5% |
| 5–20m | 5 | 19.4K | 82.8% |
| 20–60m | 4 | 29.2K | 84.1% |
| >1h | 11 | 48.5K | 79.1% |

Clean and monotonic — `spikes/cache-cadence.sh`'s mechanism holds in real use.
**08-11 was the best day: 88.1% read (iOS 89.0%, cron 85.8%), 14.3K create/turn**,
purely because 16 iOS turns arrived in three tight bursts. Nothing was tuned; the
"reasonable case" target was met by usage pattern, not engineering.

**3. Permission denials went from 0/32 to 15/57, and `had_permission_denial`
pools two unrelated things.** (Made and corrected 2026-08-12.)

- **Attended (iOS), 9/43.** These *reached the phone*. In one 08-11 window the
  journal showed **11 permission requests raised and 3 denied**, so the flag is
  mostly recording *the round-trip working*, not friction.
- **Unattended (cron), 6/14 — the real ones.** No phone, so anything reaching
  `request_decision` auto-denies. Last one then: 2026-08-10 23:55.

**Any future read must split by `source`**; for the attended half, the journal's
`permission request … created` lines are the denominator. Diagnosis of the cron
half: `docs/tasks/denied-tool-workarounds.md` §"What the first denials actually
did (2026-08-12)" — the model recovers, and says so.

**4. 16 of 57 rows had `gap_seconds: null`** (server restarted since the previous
turn): 28% unusable, an artifact of active development.

**5. Volume 4.75/day** (57 turns / 12 days), vs 3.5 at the first read. Re-read
the volume before concluding anything; 08-11 is the only day with intra-day
structure.

## Third read — 398 turns (read 2026-09-01, 2026-08-01 → 09-01)

Prompted by Mike asking what the "decouple Marlow from `claude -p`" wishlist item
would cost. The first read with enough volume to show a *trend*.

**Cache re-writing per turn fell 5x, and it is still the whole cost.**

| Week | Turns | Cache write/turn | Read:write |
|---|---|---|---|
| w32 | 109 | 111,285 | 2.22 |
| w33 | 106 | 54,366 | 2.22 |
| w34 | 107 | 21,775 | 3.46 |
| w35 | 44 | 27,706 | 3.75 |

Nobody set out to fix this: idle compaction, session rollover, the effort pin and
tool-surface trimming compounded, none of them framed as a cache fix. **Read:write
is the number to watch** (it moves with usage volume).

**Priced against the API** (the box is pinned to `sonnet`; Sonnet 5 rates $2/$10
per MTok, cache read $0.20, cache write 1.25x at 5m TTL, 2x at 1h):

| Window | Turns | API equivalent |
|---|---|---|
| Full month | 398 | $72–106/mo |
| Trailing 14 days | 233 | **$42–60/mo** |

Claude Code caches at 1h, so the top of each range is the honest figure. **Cache
writes are 78–85% of it.** Quote the trailing number (it is half the monthly
because of the trend). Against a $20/mo Pro subscription that also covers dev
work, that is 3–5x. **Decision (Mike, 2026-09-01): the subsidy wins, status quo
stands.** Don't re-open on cost grounds without a fresh read.

**Answered from the same read:**

- **Block-change misses are effectively zero.** Across the trailing 233 turns
  there are **two** distinct `identity_hash` values and **one** change between
  consecutive turns. `memory-index-split.md` and `nightly-consolidation.md` can
  stop being framed as cache problems; the remaining cost is prefix size and
  expiry, not self-inflicted invalidation.
- **`num_turns` (loop depth) shipped 2026-09-02.** It was in the result envelope
  all along: one key read in `_invoke_once`, one field in `record_invocation`.
  Rows from 2026-09-02 carry it; earlier rows have no such key, so an aggregation
  must treat missing (before the field existed) and null (no envelope)
  differently. What remains is the *question* — whether deep tool loops write the
  tokens — which needs a fortnight of rows.
- **Denial rate split by source** — answered against 386 invocations in
  `docs/tasks/marlow-harness-affordances.md` §"The re-read". Cron denials did not
  stay at zero: 21/69 cron turns (30%) vs 18/299 iOS (6%), flat at 1–2/day. The
  Bash-first habit persists (job prompts that named the tools were mostly clean,
  the two that didn't failed every run). Six denials were a *vetted* `ls` killed
  by `2>&1`, `&&`, `| tail` or quotes; not worth admitting `|`, but worth
  *saying* that a vetted command must stand alone (now in the job prompts).

## Context the log can't tell you (recorded 2026-08-12)

Everything from 2026-08-01 to 08-12 was recorded **during active development of
the thing being measured**; a reader of `usage.jsonl` alone would draw wrong
conclusions:

- **10 distinct `identity_hash` values across 57 turns; the hash changed on 9 of
  56 transitions.** That is *us editing `identity/`*, not steady state. Don't
  treat that window's block-change miss rate as a baseline.
- **The identity block changed size twice:** ~64.5K chars until 08-09, ~7.8K
  (`index_only`), ~8.9K from 08-10. **Rows before 08-10 are a different system.**
  For a like-for-like read, filter to `identity_chars < 10000`.
- **The 28% null gaps are deploys**, not crashes.
- **08-11's 18 turns include dogfooding** (the dictation work being exercised);
  its 88.1% is a best case produced by burst testing.
- Turn volume (3.5 → 4.75/day) is **inflated by the developer being the user**.

Mike asked (2026-08-12) for a re-read after several days with no dev work, to get
the first stretch of *usage* rather than usage tangled up with development. Aim
it at 08-12 onward, filtered to `identity_chars < 10000` and a stable hash, and
compare rather than pool.

**OPEN:** does the gap distribution change when the developer stops driving? The
dev window is bimodal, with cold starts the common case by turn count. If clean
usage is even more idle-dominated, warm-path tuning is dead and the answer to
cache cost is cron spacing, not prefix size.

**Data:** `ssh marlow@marlow.<tailnet>.ts.net`, then `~/.marlow/logs/usage.jsonl`.
Numbers and hashes only, so safe to copy off the box.

## Reference

`docs/marlow-v2-gate1-results.md` §8/§8b; `spikes/cache-cadence.sh` (the
synthetic counterpart). `docs/tasks/chat-poll-slow-phase.md` is a second consumer:
phase 1's wall-clock duration is what decides whether the iOS slow-poll phase
should be deleted.
