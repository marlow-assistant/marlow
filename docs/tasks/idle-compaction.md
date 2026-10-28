# Task — idle-triggered compaction

**Status: SHIPPED. Merged 2026-08-18, deployed 2026-08-19.** `server/idle_compaction.py` (`IdleCompactionWatcher`, modeled on `auth_monitor.AuthWatcher`) polls every 30s and enqueues a raw `/compact` turn against the phone's attended session when three gates pass (below). Later changes: the hysteresis baseline became the minimum reading since the last compaction (2026-08-23); the gate moved onto the true context size (2026-10-05); the idle window went from ~290s to 3000s (2026-10-18). A manual "compact now" is parked (2026-10-09).

Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

## Why

The phone's session never ends. As of 2026-08-16 it was **one session, 95 turns, five days old, a 4.78 MB transcript, no compaction marker.** Per-turn `cache_creation` climbed ~150K (08-12) to ~300K (08-16), with one turn at 594K. Over Aug 13-16, **47 turns cost ~22.1M input tokens**. The daily 07:30 cron, which starts fresh every time, averages 30-40K on the same box. Mike's read: 98% of his usage comes from long conversations sitting at 150K+ context.

The daily rollover (`nightly-consolidation.md` piece 1) caps this at 24h; this caps it continuously. Rollover changes the numbers but does not remove the need: a session can still run one heavy day on the same cost curve.

**Purpose, restated because it is easy to drift off:** compaction here is a **cold-start cost reducer, not a context-window manager.** Mike converses, context grows, he idles out, the cache goes cold, and the next message pays a full re-read of whatever the context became. Compacting to ~30K before that makes the inevitable cold read cheap. While he is still talking the cache is hot and compaction buys nothing, and blowing the window is not the problem being solved (chit-chat will not fill 1M tokens).

## The trigger — and the thing that is easy to get wrong

Mike's shape: context above a threshold, then he goes quiet, then the harness compacts.

**Measured against real usage, the idle timer is not the gate.** From `usage.jsonl` (107 attended turns with a recorded gap):

| | share of gaps |
|---|---|
| > 5 min | **38%** |
| > 10 min | 35% |
| > 30 min | 24% |
| > 1 h | 18% |

Median gap between attended turns: **2.3 min**. A bare "idle 5 min" fires after roughly four turns in ten, about eight times a day.

**So idle is permission to compact, not a reason to.** Three gates, all required:

1. **Idle**: no completed `source == "ios"` turn for `idle_compaction_idle_seconds` (default 3000s).
2. **Above threshold**: last attended turn's context >= `idle_compaction_threshold_tokens` (110,000) and >= the floor (`idle_compaction_floor_tokens`, 50,000). The floor is the tool's own reported minimum: below it the summary costs more than it reclaims. Early compaction (published guidance says 50-60% of the window rather than 95%) also leaves the model room to write a thorough summary. The figures come from third-party write-ups, not Anthropic docs; they picked defaults, and tuning goes against `usage.jsonl`.
3. **Hysteresis**: grown by `idle_compaction_growth_tokens` (27,500) since this session's last compaction. Without it every idle stretch after a big session re-compacts a session already as small as compaction can make it: repeated cost and fidelity loss, no benefit. The exact fraction is a guess; that the rule exists is what matters.

Other rules: key idle off attended turns only (a 07:30 cron turn is not Mike being active, and letting it reset the idle clock would suppress compaction exactly when the overnight gap made it cheapest); compact only the attended session; don't fire while the queue is paused for `reauth_needed`; **cancel a still-queued compaction when an attended message arrives** (`TurnStatus.CANCELLED`, wired into `WorkQueue.enqueue()` so every attended path gets it). A message arriving while a compaction is already in flight just waits for it to finish. A session awaiting its baseline is ineligible for another compaction (compacting a session of unmeasured size is the failure hysteresis exists to prevent). A dead session (`require_session=True` refuses it) is left as a visible ERROR turn, never retried.

The compaction turn runs with `source="compaction"` (not in `active_turn.ATTENDED_SOURCES`, so permission requests auto-deny), `require_session=True`, and `raw_prompt=True`, a new `invoke.py` parameter that skips `prepend_turn_context` and the attachment line, because the probe found bare `/compact` has to be the whole prompt. It is excluded from every delivery path and from `session_state` retirement. One work queue serializes all `claude -p` calls, so it cannot overlap a real turn.

### On the idle window — 50 minutes since 2026-10-18

**The cache cliff exists, at exactly one hour.** Two weeks of `usage.jsonl` (resumed chat and cron turns, 2026-10-04 to -18), as the share of the prefix each turn had to re-write:

| gap since the previous turn | turns | median | p90 |
|---|---|---|---|
| 5-30 min | 39 | 2% | 11% |
| 30-45 min | 18 | 3% | 34% |
| 45-55 min | 8 | 9% | 15% |
| 55-60 min | 14 | 3% | 6% |
| 60 min-2 h | 11 | 50% | 95% |

The longer history sharpens the edge: across all of `usage.jsonl`, 55-60 min re-wrote 1-6% on 16 turns and 60-65 min re-wrote 12-94% on 7. Outliers under the hour are most likely cron turns switching effort level, which re-caches part of the prefix on its own (`model-and-effort-config.md`). Never 100% past the hour: the system prompt and tool definitions stay warm; only the conversation goes cold. The CLI picks the one-hour TTL for subscription logins; `gate1-results.md` §8b (240s HIT, 420s MISS, 720s HIT) fits a one-hour TTL with a noisy MISS.

**What that meant for the old 290s.** The reasoning was always "compact just before the cache goes cold, so nothing warm is discarded." With a one-hour cache, 5 minutes was the worst timing: it summarized away a conversation that stayed warm for 55 more minutes, and about a fifth of Mike's gaps fall in that window. Each paid for a summary plus a fresh write of it, where the alternative was a warm read costing about a cent, and lost the verbatim context.

**Now 3000s (Mike, 2026-10-18)**, ten minutes of margin for a compaction that runs ~35s and may wait behind a cron turn. Env var `MARLOW_IDLE_COMPACTION_IDLE_SECONDS`. **Re-measure after a big CLI update**: the TTL is the CLI's choice, and a move back to 5 minutes would make 50 minutes compact after the cache is already cold (still correct, just paying a cold read for the summary).

## What signal measures "context size"

Not the CLI transcript's size on disk: it overcounts (tool output is recorded there but doesn't all ride in the prefix) and couples the server to the CLI's private layout. The probe confirmed it empirically (below): the transcript GREW while compaction succeeded.

Until 2026-10-05 the gate used `invoke.py`'s summed `cache_read`/`cache_creation`/`input` counters. See "Read 2026-08-23" for why that was knowingly wrong and why it was kept, and the next paragraph for why it stopped.

**Gate moved to the TRUE size, 2026-10-05.** The 2026-08-23 argument held only while no true number existed. The usage-visibility change added one, `InvocationResult.context_tokens`: the last top-level model call's prefix, which already includes every tool call and result exactly once. With that available the bias only cost: on 2026-10-05 a fresh 38,406-token session made 5 calls in one turn, the summed counters read ~190K, and it was compacted five minutes later, below the 50K floor, reclaiming nothing (and hiding what a longer verbatim conversation really costs; Mike: "we're really not able to see"). `_prefix_reading` now prefers the true size and falls back to the summed counters only when a turn has none, so a stream-format regression degrades to over-compacting, never to never compacting. Thresholds were not re-tuned; 110K now means 110K.

## Probe results — run 2026-08-16 on the box

`scripts/probe-headless-compact.sh`, throwaway session. **Verdict: outcome A, headless `/compact` works** (the alternatives were: slash commands not honoured in print mode, which would have meant a summarise-and-reseed primitive shared with nightly consolidation; or compaction wedging a resumable session, which would have killed the task).

- **2 `compactMetadata` + 2 `isCompactSummary` markers**: both bare `/compact` and `/compact <instruction>` really compacted.
- **`cache_read` fell 24,904 -> 18,478** across the compactions (modest, the session was tiny, but real).
- **The session resumed cleanly afterwards and kept the gist** (summarized rather than lost). Not wedged, which was the disqualifying outcome.
- Bare `/compact` as the *whole prompt* worked, confirming the `prepend_turn_context` bypass is necessary and sufficient.

**Three implementation constraints, two unanticipated:**

1. **The transcript GREW while compaction succeeded** (32,676 -> 44,660 -> 54,691 -> 66,323 bytes). The `.jsonl` is append-only; compaction *appends* a summary record. A naive implementation would read the growth as failure and compact again in a loop.
2. **A compaction call returns `subtype=success`, `is_error=false`, and an EMPTY `result`.** An ordinary turn would be marked DONE with empty text and delivered to the phone as a blank message or a pointless push. **A compaction turn must be excluded from delivery**, a new requirement on the queue, not just a flag on `invoke.py`.
3. The `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=1` arm produced no compaction. Inconclusive on its own terms (the session was ~24K, below the reported 50K floor), and it does not matter: that path is rejected on design grounds (next section).

## DECIDED 2026-08-16 — do not use the CLI's own auto-compaction

Earlier revisions floated `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` (and `CLAUDE_CODE_AUTO_COMPACT_WINDOW`) as a cheap safety net, possibly retiring the task. **Mike's call: no. Do not chase it, and do not re-probe it.** Auto-compaction fires when context crosses a threshold, which happens **during a turn, while Mike is waiting for a reply**; he pays it as added latency, at an unpredictable moment. The idle trigger does identical work when nobody is waiting. Same compaction, same token cost; the difference is entirely *who absorbs the wait*. This is the objection that ruled out threshold-triggered compaction on 2026-08-15; the env var is that idea wearing a different hat. (The CLI's own auto-compaction is still live; we declined to override its threshold, not to disable it.)

**OPEN, never settled:** the live session showed zero compaction markers in 95 turns, which means either the context never crossed the CLI's threshold or headless `-p` sessions do not auto-compact at all. The 24K probe could not test it.

## Corrected after `/code-review medium`, 2026-08-18 — the watcher only learned from successes

Three separate bugs with one root cause, and the fix is worth stating as a rule because it is easy to reintroduce. Every piece of the watcher's state (idle clock, prefix estimate, hysteresis baseline) hung off a `status == DONE` branch in `_drain`, and every one of those is a *reason to compact*. A failed turn left the watcher holding a confident, wrong picture rather than an uncertain one, so failure made it **more** eager:

1. A compaction that ERRORed never marked the session, so the next poll re-enqueued. With `require_session=True` disabling the dead-session self-heal, a stale session id fails permanently: an idle overnight session would have issued a full-context `claude -p` every 30s until morning, each billed.
2. `_last_attended_session_id` updated unconditionally while the prefix updated only when usage was reported, so a turn with no usage block paired a **new** session with the **previous** session's size. Post-rollover that reads as a fresh session already at 400K and fires a compaction reclaiming nothing (the probe's `preTokens: 657` waste).
3. The idle clock advanced only on fully successful phone turns, so an errored message left it reading from the last successful one and a compaction could fire while Mike was actively retrying.

**The rule: an outcome the watcher cannot interpret must move it toward "I don't know", which declines to compact, never leave it on a stale belief that says compact.** Both hooks (`note_attended_turn`, `note_compaction_attempted`, renamed from `*_done` because they fire on failure too) are called from *after* the try/except in `_drain`, keyed off `turn.status`/`turn.result`; the prefix is assigned unconditionally including `None`; the idle clock resets before any early return. Marking a *failed* compaction as awaiting-baseline also gives the retry a natural backoff: one attempt per `idle_compaction_growth_tokens` of growth instead of one per poll. A CANCELLED compaction is deliberately not counted as an attempt; it never invoked anything.

Left unfixed by Mike's call: a compaction already queued when the queue pauses for reauth is held at head-of-line and runs ahead of the message waiting behind it (the `paused` check is enqueue-side only). Too narrow a race to be worth a drain-side guard; revisit if ever observed.

## First production measurement, 2026-08-21/22 — and a counter that lied

The watcher fired once overnight and **reclaimed 142,967 -> 33,770 tokens (76.4%)**, and reported none of it. The `/compact` invocation's `usage` came back with its prefix keys **present and all zero**, so `_sum_prefix_tokens` returned `0` rather than `None` (it sets `found` on key *presence*), the reduction log's `> 0` guard declined to fire, and the run logged only `pre-compaction prefix=0 tokens`. The compaction worked; the evidence for it was lost. (The first "the CLI reports X" probe assumption falsified by real output; the second, a day later, was `thinking_tokens`, see `turn-activity-streaming.md`.)

Fixed 2026-08-22: `note_compaction_attempted` falls back to `_last_attended_prefix_tokens` when the compaction call's own counters are absent or zero, guarded on the session id so it never borrows another session's size. That number is better evidence anyway: measured by a real attended turn, and what `_eligible` gated on. A surviving zero logs "prefix unavailable", and the success line names its source (`via compaction call` / `via last attended turn`). **Do not treat the `/compact` response's own `usage` as a reliable pre-compaction measurement**, nor as a post-compaction one (it describes the prefix it read before appending the summary): the post-compaction size is logged from the next attended turn, as an operational effectiveness measure, not an exact summary size.

**A zero reading is not a measurement.** Recorded as a hysteresis baseline it says "this session compacts down to nothing", and the growth gate reads it literally, so every idle stretch past the floor would compact for the rest of the session.

## Judgment calls made while building (what still matters)

- **Compaction state (idle clock, prefix, hysteresis) is in-memory only, not persisted like `session_state.py`.** `session_state` needed disk because its facts must survive an hours-long gap before the 01:00 consolidate cron; the watcher just re-learns the current prefix from the next attended turn. **Re-examined against real data 2026-08-19: holds, with one measured exception.** A restart *after* the day's last attended turn leaves the watcher blind until morning (it learns only from `source == "ios"` turns): measured on the first night after deploy, the box restarted 21:13, no phone turn followed, and the 01:00 `consolidate` cold-wrote a **578,037**-token prefix (`cache_creation_input_tokens`) against 38,702 read. **Mike's call: not worth code.** On an ordinary day the watcher holds a live prefix and compacts in the evening; the gap opens only on deploy evenings and costs one cold consolidate, not an accumulating debt. **What this invalidates is the ~110K bound quoted in `model-and-effort-config.md`** for consolidate's mid-session effort switch: it is a normal-day figure, not a guarantee. A known, priced exception; don't re-derive it as a finding.
- No cap on the in-memory `_hysteresis` dict: one small dataclass per session id ever seen is not worth guarding, per the bias against speculative machinery. Revisit only if measured as a real footprint.

## Relationship to `nightly-consolidation.md`

Neither replaces the other. Rollover bounds growth at 24h and is tied to consolidation, so the day's learnings are filed before the session ends. Idle compaction bounds growth continuously and makes a skipped or failed consolidate night non-critical, which is why Mike wanted it regardless of rollover edge cases (2026-08-16).

## Noted, NOT acted on — compaction as a prompt-injection laundering step

Raised 2026-08-18 while deciding whether this branch needed a `/security-review`. **Mike's read: circuitous, and he is largely right.** Recorded so it is a considered decision and not re-raised as a novel find.

**The concern.** The session prefix contains WebFetch results and Gmail list-view output, which `~/.claude/settings.json`'s own comment calls the richest prompt-injection carrier on this box. Compaction has the model rewrite that material as its **own summary**: no longer quoted external content the model can recognise as untrusted, but first-person narration in the permanent context, original dropped. An instruction the model would treat sceptically as page content could persist as "I was asked to do X" with its provenance erased.

**Why it is probably not worth building against.** The injected content is *already in the prefix and already read by every subsequent turn* whether or not compaction runs. Compaction changes its *form*, not the exposure; the entire marginal risk is lost attribution. And it must survive three lossy steps: the text present, surviving summarization in an instruction-like form (summaries are overwhelmingly descriptive), and a later turn acting on it. **What already bounds it:** compaction turns are unattended, so `server/app.py` auto-denies any permission request they raise; a laundered instruction cannot *do* anything when laundered. The residual exposure is a later unattended turn with standing write access, i.e. nightly consolidate and its `WRITABLE_SUBTREE=memory`.

**Decision rule: do not pre-emptively harden.** Act only on something concrete: a memory in `memory/` that traces to fetched or emailed content rather than to Mike, or a compaction summary observed carrying an imperative that did not come from him.

## Read 2026-08-23 — what the first week of production actually says

*(Superseded for the gate's input on 2026-10-05, see above. Kept because the loop-depth finding still explains every summed reading in `usage.jsonl`, and the fallback path still uses them.)*

Two weeks of `usage.jsonl`, read with `scripts/context-curve.py`. **The gate had been measuring the wrong quantity since it shipped, and it did not matter** while no true number existed.

**HOLD until ~2026-08-28 (Mike): gathering real curve data.** The one question left open was the spurious-compaction rate: how often a compaction fires against a session whose smallest nearby reading was well under the 110K threshold. (`python3 scripts/context-curve.py --days 5` answers it.) Do not re-tune the threshold, add a cap, or "fix" the signal before that data exists; the analysis behind this fix went wrong twice by reasoning ahead of the numbers.

### The size signal was loop depth x size, and that was SAFE

`_sum_prefix_tokens` sums `cache_read`, which the CLI reports as a total across the invocation's internal agentic loop, not as one call's prefix (already recorded in `usage-telemetry.md` on 2026-08-10: "individual turns read 206K, 264K, 344K" against a 200K window). The curve shows it:

```
2026-08-23T10:54:45  ios  f46755dc   245,537
2026-08-23T10:54:59  ios  f46755dc    62,346    <- fourteen seconds later
```

Context did not fall 183K in fourteen seconds; the first turn made ~4 calls and the second made one. **A reading is therefore always >= the true size, never below** (loop depth is at least 1). Given the purpose, that asymmetry was protective:

| | cost |
| --- | --- |
| False positive (deep turn on a small session -> spurious compaction) | one invocation, fired while Mike is idle, possibly still cache-warm: cheap |
| False negative (large session read as small -> no compaction) | the expensive cold start this module exists to prevent |

So **do not "fix" the signal by dividing out loop depth** (`num_turns`) while the summed counters are the input: accuracy would remove the bias doing the protecting. `num_turns` is worth recording for *reading* telemetry, not for the gate. This also explained 2026-08-23 06:46, which looked like a bug: a deep turn after the 06:31 compaction read 142,028 and the watcher compacted an already-compacted session. The 76.8% and 80.1% "reductions" logged that day were artifacts of comparing a deep turn against a shallow one.

### The one place the inflation was dangerous — fixed

Frozen into the **hysteresis baseline** the bias does the opposite: one deep turn right after a compaction recorded 142,028 as the floor, and the growth gate then wanted that plus `idle_compaction_growth_tokens` before firing again, suppressing compaction for the rest of the session (unnoticed only because a restart wiped the map). **The baseline is now the minimum reading seen since the last compaction, not the first.** Nothing between compactions makes a session genuinely smaller than compaction made it, so the smallest reading is the best estimate of the floor; this is robust to the inflation and errs toward compacting. A drop can also be a real CLI-side compaction, and taking the minimum is right either way, so the code does not try to tell them apart.

### Considered and rejected in the same pass

- **Feeding cron/`system` turn sizes into the estimate.** Cron jobs that attach to Mike's session do grow it invisibly (`note_attended_turn` is `ios`-only). Built, tested, reverted: the estimate needed to be biased-high, not accurate, and already was. The apparent evidence (a "cron refill" before a 142,028 reading) was misread: that row was `ios` and no cron ran.
- **Lowering the 110K threshold toward the 50K floor.** Defensible (a smaller context is cheaper to re-read after a long idle), but a judgment about token spend wanting a few days of curve data behind any number. Parked, not dismissed.

### Reading the curve

`scripts/context-curve.py` is the reader `usage-telemetry.md` deferred. The counters were never missing from disk; grepping the server log for "compact" showed only the endpoints of each cycle, which is what hid the loop-depth inflation for two weeks. Rows sum the same three keys `_sum_prefix_tokens` gates on, so they compare directly against the server log's `prefix >= N tokens` lines, with the standing caveat that neither number is the context size.

## Read 2026-10-09 — true size, and why manual compact is parked

Five days on the true-size gate (2026-10-05 to 10-09), from `usage.jsonl` `context_tokens` and the watcher's log:

- **Idle compaction fires about once a day**, at 110-121K, always in a quiet gap, landing at 37-43K (117.7K -> 37.5K, 121.2K -> 42.6K, 110.8K -> 39.0K). No misfires since the gate moved. The "grew by 161%" and "reduced 4.8%" log lines are from 10-03/10-04, under the summed-counter gate.
- **Every day already starts fresh.** Nightly consolidation retires the session, so a new one starts each morning.
- **Most of the context comes from scheduled jobs, not chat.** A fresh session is ~26K. The morning job adds ~50K (25.7K -> 76.8K on 10-06), and each email-flash adds 5-15K. On 10-08 two chat messages, then cron alone, took the session from 48K to 122K by night: the one large session never compacted, because the gate keys on an attended turn and the nightly reset cleared it first.

**Manual compact / "start fresh, keep memory" (Mike's wishlist, 2026-09-30): parked, Mike agreed 2026-10-09.** The nightly reset is "start fresh, keep memory", run daily, and idle compaction holds the rest of the day under ~120K. The day that prompted the request ran on the old inflated gate. **What would reopen it:** Marlow visibly forgetting something after a compaction, most likely detail from the morning brief asked about after that day's compaction. If usage ever gets tight, the lever is the ~50K the morning job leaves in the conversation, which is by design (so Mike can reply to the brief), not a bug.

## Reference

`docs/marlow-v2-gate1-results.md` §8b (the cache sweep); `docs/tasks/usage-telemetry.md`; `docs/tasks/nightly-consolidation.md` piece 1; `server/queue.py` (the single queue, the auth pause).
