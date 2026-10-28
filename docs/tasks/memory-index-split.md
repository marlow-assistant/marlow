# Task — memory index/retrieve split (`inject_all` → `index_only`)

**Status: merged 2026-08-01, live on the deploy box since 2026-08-08,
verified in telemetry 2026-08-10.** `MARLOW_MEMORY_MODE=index_only`
is set via the systemd unit's `Environment=` line; `config.py`'s own default
stays `inject_all` on purpose, for the test baseline and every other install.
**One thing left, scheduled below: retire `inject_all` entirely.**

**Measured effect on the box:** `identity_chars` 65,023 → 7,833 (88% cut).
**The cache half of this was revised down 2026-08-12** — the "~52K → ~5K on a
cold cron turn" figure below rested on one small row read on 08-10. With 13 cold
starts either side of the change, the *floor* fell ~36.8K → ~15.8K tokens (about
the block's own size, which is the honest win) while the *median* cold start
barely moved, because `cache_creation` also grows with agentic-loop depth. The
direction holds; the magnitude does not. See
`docs/tasks/usage-telemetry.md` §"Second read — 57 turns". (Note: the original case
for this task cited a since-corrected 156KB/58K-token memory-size figure from
gate1 that turned out to include v1's `logs/` — the real win is smaller than
originally argued but the direction and the measured cut are both real; see
`marlow-v2-gate1-results.md` §8b item 4 if the history matters.)

**Why, in one line:** an injected memory block sits in front of conversation
history in the cache prefix, so a large `inject_all` block makes every memory
write an expensive cache miss for the *whole* history behind it — an index
plus on-demand `Read` shrinks that miss to noise. Index format is byte-stable
by construction (deterministic ordering, no timestamps/counts/hashes) so
adding one entry changes exactly one line, not the whole block's shape.

## Retiring `inject_all` — still open, the one remaining action here

`MARLOW_MEMORY_MODE` exists as a rollback for an **unvalidated behavioural
bet** — nobody had yet seen whether "index + go read the file" beats
answer-momentum at real memory size when this shipped. **Rule: delete
`inject_all`** (the constant, the branches in `_read_memory_entries`, the mode
setting) **once `index_only` has been the default on the box for two weeks
with no rollback.** Check `usage.jsonl` over that window for a smaller, stable
`identity_chars` with no rise in error subtypes. Scheduled deletion, not a
permanent fork — the mode-dependent code is ~10 lines either way.

## Reference

`docs/marlow-v2-gate1-results.md` §8/§8a/§8b/§8c (the cache-prefix and
context-occupancy arguments); `docs/tasks/loader-identity-injection.md`
(original inject-vs-retrieve axis); `docs/tasks/cache-friendly-identity-injection.md`
(interacts, doesn't subsume); `docs/tasks/usage-telemetry.md` (how the
two-week retirement check gets its numbers). Full original design
deliberation (index-line format options, migration script details, test list)
is in git history if ever needed — none of it is live reference material now.
