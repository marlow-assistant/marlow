# Marlow v2 — GATE #1 Results (CLI contract spike)

**CLI version tested:** `claude` **2.1.211** (Claude Code), confirmed identical on
Mike's macOS laptop and the Ubuntu server (`mike@…`). Results below hold for this
version; re-run this spike on any CLI bump (see §9 of the implementation plan).
**Date:** 2026-07-16. **Method:** run by hand in a clean SSH/terminal shell — *not*
nested inside an agent session (see finding 5).

This file is the empirical source of truth for the `claude -p` behaviors the server
depends on. The implementation plan's `⟨needs GATE#1⟩` markers resolve to what's here.

---

## 1. Headless permission behavior + `permission_denials` shape — CONFIRMED

A tool that requires permission in `claude -p` is **auto-denied, does not hang**.
It surfaces in a top-level `permission_denials` array in `--output-format json`:

```json
"permission_denials": [
  { "tool_name": "Bash",
    "tool_use_id": "toolu_…",
    "tool_input": { "command": "whoami", "description": "Print current user" } }
]
```

**CRITICAL for `invoke.py`:** on a denial the run still reports
`is_error=false`, `subtype="success"`, **exit code 0**. The server therefore
**cannot** detect a denied tool from exit code, `is_error`, or `subtype` — it
**must inspect `permission_denials` for non-empty**. The assistant's `result`
text also narrates the denial ("I need permission…") but that's model prose, not
a reliable signal — key off the array.

## 2. PreToolUse hook — `deny` and `allow` BOTH work in headless, with one rule

- **Hook `deny` works** — fail-closed is real. (Confirms the timeout fallback path.)
- **Hook `allow` works** — a PreToolUse hook *can* grant a tool that would
  otherwise be gated in headless mode. **§3.1(b) is validated.**
- **The one rule:** the tool must **fall through to the hook** — it must **not**
  also be listed in `permissions.ask`. An `ask` entry auto-denies in headless and
  **overrides the hook's `allow`**. This was the false-negative in early probes:
  `ask:["Bash"]` + hook-allow → denied; remove the `ask` rule → hook-allow runs.
- **Both hook output schemas are honored:** the current
  `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"|"deny"|"ask","permissionDecisionReason":"…"}}`
  **and** the legacy `{"decision":"approve"}`. Use the current schema.

## 3. Hook timeout / wait ceiling — no sub-90s cap

- Claude **waits for the hook process to exit** before proceeding; it does not
  kill it at ~60s. A hook that slept **90s** then returned `allow` was **honored**
  (wall-clock ~94s, tool ran). Every denial observed in testing was caused by the
  `ask`-rule interaction (finding 2), **never** by a timeout.
- **Design consequence:** the wait is bounded by **our own hook script**, not by
  Claude. The Marlow hook should self-timeout (e.g. `curl --max-time 55` to the
  server; on no answer → emit `deny` and let the server post "skipped it" to chat)
  and **exit promptly** once decided — a hook that lingers stalls the whole
  invocation for its full duration, since Claude waits for it.
- The per-hook `timeout` field in settings.json is available if we ever want to
  raise a ceiling, but at 2.1.211 we didn't hit one below 90s.

## 4. Corrected §3.1 permission model (supersedes R1's framing)

R1 assumed the `permissions.ask` list would *drive* the phone round-trip. It's the
opposite:

- **`permissions.allow`** — tools that run unattended, no prompt.
- **`permissions.deny`** — hard blocks (credential paths, `find … -exec`, etc.).
- **`permissions.ask`** — tools you're content to **auto-deny when unattended**
  (cron). *Not* the phone-round-trip mechanism — an `ask` never reaches a human in
  headless; it just denies. Keep this list for the strict cron policy.
- **The PreToolUse hook (fall-through tools)** — *this* is the phone round-trip.
  Sensitive-but-attended tools are left off all three lists so they fall through to
  the hook, which calls the server → phone → returns `allow`/`deny`. The hook
  self-bounds the wait and fails closed.

So: **cron sessions** run with the sensitive set in `deny`/`ask` (never prompts).
**Attended (iOS) sessions** run with those same tools left to fall through to the
hook. Same settings.json can express both by swapping which policy file is active
per session source (the server already routes by source, §3.2).

## 5. Environment hygiene — the server must scrub its env before `claude -p`

A `claude -p` launched as a child of an existing Claude Code/agent session inherits
`CLAUDECODE=1`, `CLAUDE_CODE_CHILD_SESSION=1`, `CLAUDE_CODE_SESSION_ID`,
`CLAUDE_CODE_ENTRYPOINT`, `AI_AGENT`, etc., and comes up with the **parent's tool
registry instead of a normal toolset** (no real Bash — observed it improvising a
phantom "Monitor" tool). `invoke.py` must launch `claude -p` with a **clean,
explicit environment** (scrub the `CLAUDE_CODE_*`/`CLAUDECODE`/`AI_AGENT` vars) so
the sandbox tool contract is deterministic. Also pass `--strict-mcp-config` unless
specific MCP servers are intended, and feed `</dev/null` on stdin.

## 6. Auth token lifecycle — CONFIRMED (from the server's `.credentials.json`)

- **Two-token OAuth** under `claudeAiOauth`: `accessToken` (short-lived, ~8h) and
  `refreshToken` (~9 days), plus readable `expiresAt` / `refreshTokenExpiresAt`
  epoch-ms fields, `subscriptionType: "pro"`, minimal `scopes`.
- **Headless `claude -p` auto-refreshes the access token** using the refresh token
  and **writes the new token back** to `.credentials.json` — no interactivity
  needed hour-to-hour. Verified: forcing `expiresAt` into the past then running a
  headless call advanced `expiresAt` ~8h.
- **`refreshTokenExpiresAt` did *not* visibly roll** on that refresh (same
  whole-second value). Provisional read: the refresh window is **anchored, not
  rolling** → interactive re-auth (`claude` login paste, over SSH) needed roughly
  every ~9 days. Confirm by watching the field over the coming week; if still
  anchored, ~weekly re-auth is the accepted routine.
- **`auth_monitor.py` watches `refreshTokenExpiresAt`** (not `expiresAt`),
  pre-warns before it lapses, and on an auth-failure signature stops draining the
  queue and pushes a `reauth_needed` nudge. *(gate-#1 sub-item still open: the exact
  stderr/exit signature of an expired **refresh** token — capture it the first time
  a real expiry happens, or by faking `refreshTokenExpiresAt` into the past.)*
- **Credential write-back on refresh is another reason for the single work queue**
  (§3.2): concurrent `claude -p` calls could race the `.credentials.json` rewrite.
- **Storage differs by platform:** macOS → login Keychain (`Claude Code-credentials`);
  Linux → `~/.claude/.credentials.json`. The server is Linux, so the file form is
  the one that matters.

## 7. Incidental finding (for GATE #2, the permission audit)

The server's live `~/.claude/settings.json` has an ineffective rule: Claude Code
warned `Write(/home/mike/claude-assistant/**)` is not matched by file-permission
checks — it should be `Edit(/home/mike/claude-assistant/**)` (Edit rules cover all
file-editing tools). Fix during the §5.1 permission audit.

## 8. `--append-system-prompt` caching behavior — CONFIRMED (2026-07-24)

Tested from a plain terminal (not nested in an agent session, same rule as
finding 5) while building the identity/memory loader
(`docs/tasks/loader-identity-injection.md`). Three clean-shell scripts, chained
`--resume` calls against the same session, reading `usage.cache_creation_input_tokens`
/ `usage.cache_read_input_tokens` from `--output-format json`.

- **`--append-system-prompt` content is NOT part of the resumable session
  state.** A secret string appended on turn 1 and *not* repeated on a
  `--resume`'d turn 2 is completely gone — the model has no trace of it. It
  must be **resupplied on every single invocation** for identity to be present
  on every turn. (This confirms `invoke.py`'s current every-call injection is
  necessary, not excessive.)
- **Ordinary conversation history survives `--resume` independent of the
  system prompt.** A fact planted as a normal user turn was correctly recalled
  on later turns even as the appended system prompt changed underneath it.
  History and system-prompt injection are separate mechanisms.
- **Resupplying a byte-identical `--append-system-prompt` string cache-hits
  cheaply.** Turn-over-turn with no change: `cache_creation_input_tokens`
  collapsed from ~11.5K to ~38, `cache_read_input_tokens` absorbed the
  difference. As long as the injected block doesn't change, repeating it every
  call is nearly free.
- **Any change to the injected string — anywhere in it — forces full
  recreation, not an incremental diff.** Changing a **22-character** tail on a
  ~16.5K-token block still cost ~16.2K tokens of fresh `cache_creation` (same
  order as the original cold write) and dropped `cache_read` back to the
  pre-injection baseline. Confirmed with a second script that held a large
  prefix byte-identical and only varied a small suffix — a longest-common-
  prefix cache would have shown a small miss; it didn't.
- **Conclusion: the CLI treats the entire `--append-system-prompt` string as
  one opaque cache unit.** There is no cache breakpoint between sections of it.
  Ordering stable content before volatile content inside that single string
  buys nothing — a change anywhere invalidates the whole block.
- **Design consequence:** anything injected via `--append-system-prompt` must
  be **byte-identical turn-to-turn within a session**, or every turn after an
  edit pays a full-size cache-miss. Since the current identity loader
  (`server/identity.py`) re-reads `memory/` fresh from disk on every
  invocation, a memory write mid-session silently makes every subsequent turn
  in that session expensive. See `docs/tasks/cache-friendly-identity-injection.md`
  for the fix.

### 8a. What the block sits in *front* of (added 2026-07-25)

The measurements above under-report the cost of a change. `--append-system-prompt`
is at the **front of the prefix**; conversation history — which this same finding
confirms is a separate mechanism that survives `--resume` — comes *after* it.
A prefix cache invalidates everything downstream of the change, so editing the
identity block mid-session costs the block **plus a re-write of all accumulated
conversation history**, not just the block. The §8 numbers are consistent with
this: `cache_read` fell back to the *pre-injection baseline* rather than merely
dropping by the block's size.

On a long attended session the history half is the larger cost, and it grows as
the conversation does. This is the strongest argument for freezing the string
per-session — stronger than the token count of the block itself.

**The general rule this yields, which governs all identity/memory design:**

> **The appended system prompt carries stable content only. Anything volatile,
> retrieved, or recency-sensitive enters through conversation history (as tool
> results), which is append-only and caches incrementally.**

This is *why* the memory index/retrieve split is the real fix and not merely a
context-size optimization: it puts the routing table (stable) in the prefix and
the entry contents (volatile, occasionally needed) in history, where churn is
cheap by construction. Tracked in `docs/tasks/memory-index-split.md`.

Corollary caught while writing that task: an index is exactly the kind of
artifact that naturally carries mtimes, "last updated", or entry counts. Any of
those makes it jitter on every memory write, reintroducing whole-block
invalidation *and* taking history with it. Byte-stability is a hard spec
constraint on the index format, not a nicety.

### 8b. MEASURED 2026-07-25 — cache lifetime is NOT a clean cliff

Every §8 measurement had been taken on chained `--resume` calls **seconds
apart**, so "resupplying an identical string is nearly free" was confirmed only
for back-to-back turns. `spikes/cache-cadence.sh` sweeps widening gaps with a
byte-identical 156KB block (sha-pinned, so stability is verified not assumed).
Run from a plain terminal on the dev Mac.

```
block: 156092 (ARG_MAX 1048576)           block sha 2200be505e1a7526
       ^ as printed by the 2026-07-25 script: a CHARACTER count, not bytes.
         Real argv bytes are somewhat higher (UTF-8 identity markdown).
         Fixed 2026-07-29 to use `LC_ALL=C wc -c`; a re-run prints true bytes.

TURN         GAP   CACHE_CREATE   CACHE_READ   OUTPUT   WALL   VERDICT
cold          0s          58244         7379        4     3s   MISS   (expected)
resume-1      0s             16        65623        4     3s   HIT
resume-2     90s             16        65639        4     3s   HIT
resume-3    240s             16        65655       18     3s   HIT
resume-4    420s          58200         7379        4     3s   MISS
resume-5    720s             16        65579        4     2s   HIT
```

**There is no monotonic expiry cliff.** A 420s gap missed; a 720s gap hit. A
5-minute TTL with refresh-on-access explains every row *except* resume-5; a
1-hour TTL explains every row *except* resume-4. Neither single-TTL model fits.

**Working hypothesis (Mike, 2026-07-25) — best fit to the data:** a ~5-minute
window is the *guaranteed* floor, and retention beyond that is **opportunistic,
depending on cache capacity/traffic**. ("That's the way I'd build it — 5m, but
you get more if we have cache room.") That explains both anomalous rows with one
mechanism: 90s/240s fall inside the guarantee and hit deterministically; 420s and
720s are both outside it, and differ only in whether there happened to be room.
Unverified, and Anthropic makes no such contract publicly — treat as a model
that fits the evidence, not a documented behaviour.

**The design rule that follows, and it holds regardless of which model is
right:**

> **Design against a 240-second dependable window. Anything longer is a bonus
> that must never be planned around.**

**Why 240s and not "5 minutes":** 240s is the *longest gap actually measured to
HIT*. 300s was never sampled, and the next sample up (420s) MISSED — so the
band between 240s and 420s is untested, and "5 minutes" was interpolation
dressed as a result. Schedule against the measured number. **Confirm anything
larger before fixing a schedule to it** — the flat-interval re-run below is what
would settle it.

**Structural finding — at least two independent cache segments.** `CACHE_READ`
on both misses is exactly `7379`, identical to the cold turn, while hits read
`~65623 ≈ 58244 + 7379`. So the CLI's own system-prompt/tools segment (7379
tokens) never missed once across the entire run, while our injected identity
block (58244) did. The appended block is cached separately from the CLI's own
prefix — consistent with multiple breakpoints, possibly with different TTLs.

**Cost of a miss: ~58K tokens of cache creation**, taking ~3s wall either way
(latency is not the tell — token accounting is).

#### What this settled

1. **The conversational case is safe.** 90s and 240s both HIT. The primary
   "reasonable case" works as hoped.
2. **Caching cannot be depended on.** A miss at 7 min while 12 min hits means
   the system must be correct and acceptable when the cache misses, always.
3. **The freeze task keeps priority** (`cache-friendly-identity-injection.md`,
   START slot 0b). Byte-stability demonstrably pays across realistic gaps, so
   it's worth *more* than it looked when the cliff was unknown — this resolves
   the 0b-vs-0c ordering question that was left open.
4. **The index split's argument shifts, and strengthens** (`memory-index-split.md`).
   At 156KB of memory the block is **58,244 tokens — ~29% of a 200K context
   window, occupied on every single turn, hit or miss.** Caching makes it cheap;
   it does not make it small. Attention dilution and context headroom are
   entirely unaffected by cache state. That was the original argument in
   `loader-identity-injection.md` and it is now the dominant one.

   > **CORRECTION 2026-08-10 — "156KB of memory" is wrong, and it propagated.**
   > The measurement stands; the *attribution* does not. The 156,092-char block
   > was **every `.md` in the v1 identity repo** (`find . -name '*.md'` totals
   > **151,759** bytes there today), and the bulk of it is **`logs/`** — three
   > large day-log files, 405KB on disk. Real `memory/` is **60,982 chars**:
   > nine entries plus a 723-char `index.md`.
   >
   > **v2's loader does not read `logs/`.** Marlow's own reported block on the
   > box is `identity_chars: 64517` ≈ memory (60,982) + `SOUL.md`/`USER.md`
   > (5,724). So v2's block was already **~59% smaller than this row's number
   > before `index_only` was ever flipped**, and "~29% of a 200K window" has
   > never described a v2 turn. `index_only` replaces only the memory portion,
   > so its saving is real (lint: 1,961 index chars vs 59,224) but it is
   > ~15K tokens of headroom, not ~58K.
   >
   > The conclusion is unchanged — the split is still worth it, for exactly the
   > attention/headroom reason given above. Only the size of the prize moved.
   > This row is also where the retired next-session doc's "the seed may be
   > missing memories" scare came from; it is settled and there is no gap.
5. **Cron spacing:** 240s gaps demonstrably share cache, so a tight morning
   batch works. Hourly spacing (v1's habit) is confirmed wrong. Keep the batch
   inside the measured 240s dependable window where possible.
6. **argv headroom is fine** — 156KB against a 1MB `ARG_MAX` (§8c).

#### Still worth one more run

The sweep confounds gap length with elapsed session time, and one run cannot
distinguish transient eviction from a systematic boundary. A **flat interval**
(e.g. `GAPS="360 360 360 360 360"`) repeats the same gap and would show whether
misses beyond 5 minutes are random (supporting the capacity hypothesis) or
systematic. Cheap; do it before relying on any number above 5 minutes.

The workload profiles this was checked against — Mike's stated "optimize for the
reasonable case, accept the idle miss" targets (2026-07-25):
- **Morning batch — CONFIRMED workable.** Cron jobs spaced inside the dependable
  window share one cached block (240s hit). v1's habit of hourly-spaced morning
  tasks (7:45 ping, 9:00 email scan) was close to worst-case: far enough apart
  that every job paid a full ~58K cold write, with no user-visible benefit from
  the spacing. Prefer fewer, coarser-grained jobs, or several inside one short
  window.
- **Daytime conversation — CONFIRMED.** Turns 1–2 minutes apart land as HIT
  (90s measured). The injected-identity design holds for the case that matters
  most.
- **3-hour idle → cold.** Accepted, not worth engineering against. (Not
  separately measured — nothing in the data contradicts it, and the cost is one
  ~58K write on the first turn back.)

### 8c. Side-finding: argv is a hard ceiling on injected identity

`server/invoke.py` passes the composed block as a **command-line argument**.
`ARG_MAX` is ~1MB on this Mac and typically ~2MB on the Ubuntu deploy host, but
it is shared with the whole environment block. Today's ~156K characters of
memory (somewhat more than that in UTF-8 bytes — the 2026-07-25 run counted
characters; see the note under §8b's table) is comfortably clear at a ~6x
margin; an inject-all strategy against a memory dir that keeps
growing is not indefinitely safe. `spikes/cache-cadence.sh` prints block size
against `getconf ARG_MAX` and warns past half. One more reason the index split
is the durable answer. (If injected identity ever must exceed this, the fix is a
different injection mechanism, not a bigger argv — reopen the gate #1 contract.)

---

## Bottom line

Gate #1 **passes**: the headless permission contract, the hook-based phone
round-trip (both grant and deny), the no-sub-90s wait ceiling, and headless auth
self-refresh are all confirmed on 2.1.211. The one design correction is finding 4
(the `ask` list is the cron auto-deny layer, **not** the round-trip; the hook is the
round-trip). Finding 8 (added 2026-07-24, extended 2026-07-25) is a real
follow-up item, not a blocker: `--append-system-prompt` caches as one opaque
block sitting in front of conversation history, so the identity loader needs to
keep it byte-stable within a session (`docs/tasks/cache-friendly-identity-injection.md`)
and ultimately small (`docs/tasks/memory-index-split.md`). One sub-item is still
**unmeasured** — how long the cache entry survives between turns (§8b,
`spikes/cache-cadence.sh`); it decides how much the freeze is worth and how the
morning crons should be spaced. Nothing else here blocks the build; Sonnet can
proceed to server-skeleton work with the invoke/permission contract now pinned.
