# Marlow v2 — build log (shipped work, routing)

**This is the archive, not the mandatory read.** `marlow-v2-START.md` is
the router every session reads; this file is what it routes *to* for anything
already shipped. Split out 2026-08-19 because the detail had grown into a second
copy of every task doc's Status line inside a doc whose own preamble says it
"routes; it does not explain." Compressed 2026-10-26 to routing and the traps not
recorded elsewhere.

**Each task doc's own Status line is authoritative.** Where this file and a
`docs/tasks/*.md` disagree, the task doc wins and this one is stale. Many rows
below were written before their doc closed out; read the doc for current state.
Nothing here is load-bearing for picking up new work.

## Gates and the first build

Everything below is ✅ **done and merged**. Detail is in the linked doc, not here.

| | what | where the detail lives |
|---|---|---|
| Gate #1 | CLI-contract spike, passed on `claude` 2.1.211; contract pinned | `gate1-results.md` |
| Gate #2 | Permission audit; deny layer on interactive settings | `gate2-results.md` |
| Gate #3 | Server skeleton, ingress, liveness, `deploy/` artifacts | `gate3-results.md` |
| Repo split | v2 out of the monorepo; empty `identity/` template, seeded at deploy | handover doc |
| Gate #4 | Permission round-trip, code + hand-verified over real HTTPS | plan §4 |
| TLS | Public listener requires TLS or refuses to start | `deploy/README.md` §8b |
| Auth-scrub + queue pause | `invoke.py` scrubs three exact `ANTHROPIC_` names (**not** the prefix — deliberate, see its docstring); head-of-line turn held PENDING while `reauth_needed` | plan §6a |
| P1 cron | `POST /internal/cron/message`, `cron/entrypoint.py` | `tasks/scheduled-work.md` |
| P2 APNs push | Real dispatch and token storage | `tasks/apns-push-delivery.md` |
| P3 iOS client | Chat + pairing + macOS CI gate (since turned off; see CLAUDE.md) | `tasks/ios-minimal-client.md` |
| Identity loader | `identity.py` → `--append-system-prompt`, frozen per session | `tasks/loader-identity-injection.md` |

Two gate-#4 caveats worth carrying: the timeout/"skipped it" path was never
hand-verified (covered only by `tests/test_pretooluse.py`), and gate #4's
verification used `curl`-as-phone. Both are low-risk, neither a blocker.

## Build queue — routing, and the traps worth carrying

Each item's Status line is authoritative; this is routing only. Slots 0a–0f were
the cache-cost work, in order.

| slot | task doc | what it was, and the trap if it is not in that doc |
|---|---|---|
| 0a | cache-cadence spike | `gate1-results.md` §8b |
| 0b | `cache-friendly-identity-injection.md` | freeze the composed identity per session |
| 0c | `memory-index-split.md` | `inject_all` → `index_only`: `identity_chars` 65,023 → 7,833. The cache win was revised down 2026-08-12 (the "~52K → ~5K" rested on one tiny row; the *floor* fell, the *median* barely moved) — `usage-telemetry.md` "Second read". The repo default stays `inject_all` on purpose (test baseline); the box sets `index_only` in the unit |
| 0d | `usage-telemetry.md` | per-invocation token/cache log at `~/.marlow/logs/usage.jsonl` (the *log* dir). Phase 2 deliberately unbuilt. `had_permission_denial` pools attended and unattended denials; split by `source` before reading it |
| 0e | `nightly-consolidation.md` | session attach (2026-08-10) and rollover (2026-08-16, job at `0 1 * * *`). Rollover is a consequence of consolidation, never an age check. **Keep state on disk** (`server/session_state.py`): the in-memory version failed silently within a day of a restart |
| 0f | `idle-compaction.md` | raw `/compact` when idle, above threshold, and grown since the last. The CLI transcript **grows** on compaction (use `usage` counters, not file size). Watcher state must move on *every* terminal outcome, or failure makes it more eager |
| — | `model-and-effort-config.md` | model/effort pinned (Sonnet, low), cron medium, per-turn effort from the phone. The compaction "current level" rule was struck 2026-08-27 (Probe 3). Server half: `effort-toggle-server-half.md` |
| — | `concurrent-send-session-chaining.md` | superseded by `one-conversation.md` |
| — | `ios-permission-round-trip-ui.md` | on-device confirmed 2026-08-03/04; a first locked-phone retest was inconclusive because of Sleep Focus, not code |
| — | `marlow-working-dir-git-policy.md` | the assistant's working dir gets a remote (`marlow-identity`), server-side auto-commit, hourly push. The pusher runs as `marlow`, not root; the Mac-side bundle (`scripts/backup-identity-bundle.sh`) is what makes a write-capable key acceptable |
| — | `bundle-backup-ssh-access.md` | the Mac-side bundle backup; now scheduled, with a staleness alert |
| — | `ios-notification-open-and-content-swap.md` | tapping a push opens a stale transcript (A) and the generic push body (B); shipped (A 2026-08-10, B 2026-09-08) |
| — | `multi-device-permission-routing.md` | placeholder; any paired device sees any pending permission request |
| — | `marlow-settings-permissions-policy.md` | `deploy/marlow-settings.json` plus a drift test. The live policy is a *copy* at `~/.claude/settings.json`; `update.sh` syncs it only since 2026-09-06 |
| — | `coalesce-queued-turns.md` | fold type-ahead messages into one invocation |
| — | `ios-unsolicited-turns.md` | the first `morning` cron reply was never fetched; fixed (merged 2026-08-08) |
| — | `ios-chat-ux-gaps.md` | the open iOS gaps. Numbering is permanent (Swift comments cite it). Consolidated 2026-09-08 because shipped write-ups were crowding out what was still open: the failure mode of appending to a doc and never retiring from it |
| — | `cron-script-confinement.md` | deferred; a promoted cron script runs outside bwrap |
| — | `marlow-harness-affordances.md` | closed by disproof 2026-09-06: wording was not the lever. 30% of cron turns hit a denial vs 6% of iOS |
| — | `schedules-and-reminders.md` | the server, not the model, owns the crontab. Debian's cron does not implement `CRON_TZ`, so the harness converts to system-local time itself. **A capability is not shipped until the thing that must use it has been told:** the identity still forbade cron changes and Marlow reached for a scheduling skill and `sleep 300` |
| — | `schedule-timezone-following.md` | answered by the above. The drift notice fires only when the two zones' *offsets* differ, and asks rather than auto-changes (a transient phone zone would silently retime the morning scan). **Lesson from review:** the store path had been re-spelled per reader (three copies); a re-spelled path fails silently on a later move with every test green. `settings.schedules_path` is the one definition |
| — | `deferred-cron-review-findings.md` | finding 1 shipped (`enqueue()` was called from sync routes, off the loop that owns the queue). `/code-review high` found `/v1/pending` had the same bug; the file's own fix sketch was wrong. Findings 2–4 are cosmetic and not filler to self-assign |
| — | `failure-alert-channel.md` | v2 has one outbound channel to Mike (a cron turn delivered by the app); decision deferred |
| — | `denied-tool-workarounds.md` | closed; keeps the decision rule (coach rather than allow when a vetted equivalent exists) and how to redo the read |
| — | `resolve-paths-once.md` | `bash_policy.anchor_to_tree()` / `reaches_protected_path()` built 2026-08-06; the type-level half is open |
| — | `probe-suite-doctor-mode.md` | idea, held until a second occasion to run it |
| — | `voice-mode.md`, `ios-modern-on-device-dictation.md` | push-to-dictate, then iOS 26 `SpeechTranscriber` (2026-09-30). **Lesson:** the first spike beat Apple's *old* API and nearly shipped a 465 MB feature on that; check the platform's current API before building around its predecessor. Touching dictation: do the device smoke check in the task doc. Spoken replies and the big-button screen are `voice-mode-hands-free.md`; its voice, Phonon on the Neural Engine, is `voice-mode-tts-engines.md` |
| — | `cli-auto-memory.md` | the CLI's own memory feature competed with Marlow's. `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` goes in `invoke._FORCED_ENV` *after* the scrub, never the unit file. A directive the model is given and cannot obey is worse than none |
| — | `ios-image-input.md`, `ios-app-badge.md` | image attachments; unread badge |
| — | `ios-transcript-scaling.md` | the transcript is one file rewritten whole. A cold-launch bug (LazyVStack realizing zero rows on device) did not reproduce in the simulator at 120 or 2500 messages; the fix is a rendered window (~48 hours) over an intact file, never a return to `LazyVStack` |
| — | `ios-unify-transient-retry-classification.md` | backlog; two definitions of "worth retrying" in `ChatStore` |
| — | `chat-poll-slow-phase.md` | deletion candidate |
| — | UX affordances (merged 2026-09-08) | found by Mike *using* the app. (a) The typewriter replayed finished replies; the rule is "type only what arrived while we were watching", keyed on "joined late", not "first ingest" (`streaming-reply-text.md`). (b) A fetch behind a notification tap showed nothing for up to 60s; now a spinner at the foot of the conversation, with a 600 ms grace so a healthy link doesn't flash it. **"Can't reach Marlow" is a claim about the network, so only a `URLError` produces it**; a 500 from `/health` means the box is at fault, not the tailnet. (c) Assistant replies out of bubbles. Only (c) was eyeballed on device at the time |
| — | `tests/test_queue.py` flake (merged 2026-09-08) | `test_drain_evicts_old_turns_end_to_end` failed once in a pre-push run. With CI off, the pre-push hook is the only gate, and a gate that fails at random is how `--no-verify` becomes a habit, so it was established rather than retried. The assertion ran straight after the last turn went terminal, before the eviction pass; it only passed by a coincidence of timing. **The lesson: an assertion satisfied by coincidence rather than by the pass under test can't be caught by re-running; instrument the thing you believe is happening.** The fix waits for the eviction pass before `stop()` |
| — | `deploy/bootstrap.sh` | not started; consolidate the proven install glue into one idempotent script so host #2 is repeatable (Mike asked for it) |
