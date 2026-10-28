# Marlow v2 — Implementation Plan

**Status:** Ready to build. This is the executable handoff.
**Audience:** The Claude Code session (Sonnet) that builds Marlow v2.
**Companion doc:** `marlow-v2-design.md` is the *rationale record* — read it only
if you must understand *why* a decision was made. It is intentionally left noisy
(it preserves superseded options and correction trails for auditability) and
will mislead an implementer who reads it as instructions. **This plan is
self-contained; build from it.** Section pointers like *(rationale: design §3.1)*
are breadcrumbs for "why," not required reading.

---

## 0. What you are building, in three sentences

An always-on Python server on Mike's Linux host is the single entry point for
every Claude Code invocation — from a custom SwiftUI iOS/macOS app and from cron.
It shells out to headless `claude -p` (keeping the Claude Pro subscription, **not**
API billing), serializes all invocations through one work queue, and routes the
rare tool-permission question to the phone via a bounded round-trip. The app adds
on-device whisper.cpp speech-to-text and push notifications; the server never has
a public listener (Tailscale-only) and treats its own security boundaries as if it
will one day run inside NemoClaw's OpenShell sandbox.

**Non-negotiables (decided, do not relitigate):**
- CLI (`claude -p` / `--continue` / `--resume`), **never** the Agent SDK — the SDK
  forces API-key billing. *(rationale: design §7)*
- One server-mediated **work queue**; only one `claude -p` runs at a time.
  *(rationale: design §3.2)*
- Server backend is **Python**. Client is **native SwiftUI**, one multiplatform
  project (iOS + macOS). *(rationale: design §8)*
- **No public ingress** — Tailscale tailnet only, device-token auth.
  *(rationale: design §10.1)*
- Build **standalone but "OpenShell-shaped"** (see §5); NemoClaw is deferred, not
  targeted now. *(rationale: design §9.2/§9.3)*

---

## 1. The empirical checkpoint — GATE #1 (DONE 2026-07-16, CLI 2.1.211)

> **✅ GATE #1 passed.** Ran by hand on `claude` 2.1.211 (identical laptop + server).
> Full results and the resulting contract are in **`marlow-v2-gate1-results.md`** —
> read that alongside §4 below. Headline corrections it produced: the phone
> round-trip is driven by tools **falling through to the PreToolUse hook**, not by
> `permissions.ask` (an `ask` auto-denies in headless and overrides a hook allow);
> there's **no sub-90s wait cap** so the hook self-bounds the wait; a denial reports
> `exit=0`/`is_error=false` so detect it via a non-empty `permission_denials` array;
> and `invoke.py` must launch `claude -p` in a **scrubbed env** (child invocations
> inherit `CLAUDE_CODE_*` and get the wrong toolset). Auth self-refreshes headlessly;
> watch `refreshTokenExpiresAt` (~9-day, likely anchored) for interactive re-auth.
> One sub-item still open: the exact failure signature of an expired *refresh* token.

Original checkpoint text (retained for context — the three behaviors that were
unverified and have now been observed):

1. **Headless permission behavior.** Run `claude -p` against a command that would
   normally prompt. Confirm it auto-denies (does not hang/queue), and capture the
   exact `--output-format json` shape — specifically the `permission_denials`
   array.
2. **PreToolUse hook timeout/deny semantics.** Wire a trivial `PreToolUse` hook
   that sleeps, and observe: what is the hook's kill timeout on this version? What
   does Claude Code do when the hook exceeds it — deny, allow, error? The bounded
   round-trip in §4 depends entirely on this being "bounded wait, then deny."
3. **Auth-expired signature.** Determine what `claude -p` emits (exit code /
   stderr / JSON) when the stored OAuth token is expired, and whether
   `~/.claude/.credentials.json` exposes a readable expiry timestamp to pre-warn
   on. *(rationale: design §3.7)*

**Record the observed facts in a short `GATE1-RESULTS.md` in this repo.** Sections
of this plan marked **⟨needs GATE#1⟩** get their final specifics from that file.
Do not guess these — if the observed behavior contradicts the plan, stop and flag
it; it may change the permission approach.

Everything in §2, §3, §5 is safe to build **before** gate #1. §4 (the permission
round-trip) and the auth-monitor are the parts that wait on it.

---

## 2. Server architecture & module layout

Python 3.12+, asyncio, single long-lived process. HTTP via FastAPI+uvicorn (or
aiohttp — implementer's call; FastAPI assumed below). One process, no worker fan-out
— concurrency is handled by the queue, not by parallelism.

```
server/
  app.py            HTTP app + route wiring (public tailnet API + loopback internal API)
  queue.py          THE work queue. Single asyncio task drains it; only one
                    claude -p invocation in flight at a time. All invocations —
                    iOS, cron, anything — enqueue here. (design §3.2)
  invoke.py         The ONLY module that shells out to `claude -p`. Isolates the
                    CLI contract so a future SDK/API-key path is a swap, not a
                    rewrite. Owns: session routing (--continue/--resume), JSON
                    output parsing, permission_denials extraction, auth-expired
                    detection. (design §7 dependency-isolation note)
  permissions.py    Permission round-trip registry: request_id -> asyncio.Future.
                    Single-use decisions, bounded timeout, fail-closed. (design §3.1/§10.4)
  pairing.py        Device pairing + token store. (design §10.1)
  push.py           APNs dispatch. Wake-and-fetch payloads ONLY — no content. (design §10.3)
  auth_monitor.py   Token-expiry detection + pre-warn. ⟨needs GATE#1⟩ (design §3.7)
  health.py         /health endpoint + status surface for watchdog/dead-man's switch.
  boundaries.py     Declarative security boundaries: fs path-sets + egress host
                    allowlist. The "OpenShell-shaped" source of truth (§5).
  log.py            Redacting logger + rotation. (design §3.4/§10.3)
  config.py         Loads settings, boundaries, secrets-from-env.

hooks/
  pretooluse.py     The PreToolUse hook script referenced from settings.json.
                    Minimal: POST to loopback /internal/permission-request, block
                    on the reply, emit hookSpecificOutput.permissionDecision. (§4)

cron/
  entrypoint.py     Replaces tmux-inject. Enqueues a prompt under the strict
                    unattended (never-prompt) policy. (design §3.1 unattended)

deploy/
  marlow.service    systemd unit: Restart=always, dedicated restricted user. (design §10.2)
  watchdog.sh       Health-probe watchdog (relaunch on wedge), self-safe. (design §10.2)
  deadmans/         Tiny job for a DIFFERENT host: curls /health, texts Mike if
                    down > N hours. Independent of the server process. (design §10.2)
  egress.nft        nftables ruleset enforcing boundaries.py's egress allowlist (§5).
```

---

## 3. iOS↔server protocol (first-cut — Mike to react to)

Transport: HTTPS over the Tailscale tailnet. All client-facing endpoints require
`Authorization: Bearer <device-token>` except pairing. APNs is a **wake signal
only**; the device pulls real content over the tailnet.

### 3.1 Public API (tailnet, client-facing)

| Method & path | Purpose | Body / notes |
|---|---|---|
| `POST /v1/pair/initiate` | New device requests pairing | `{device_name}` → `{challenge_id}`. Server surfaces an approval (see §7 open item). |
| `POST /v1/pair/complete` | Redeem approved challenge | `{challenge_id}` → `{device_token}`. Token stored in Keychain client-side. |
| `POST /v1/message` | Send a user turn (already STT'd text) | `{text, session_hint, client_msg_id}` → `202 {turn_id}`. Enqueued; response comes async. `client_msg_id` makes resend idempotent. |
| `GET  /v1/turn/{turn_id}/stream` | SSE stream of the assistant response | Fallback `GET /v1/turn/{turn_id}` for whole-response pull after a wake. |
| `GET  /v1/pending` | After APNs wake, pull anything waiting | Returns ready turns, open permission requests, and a `reauth_needed` flag. |
| `POST /v1/permission/{request_id}` | Answer a permission question | `{decision: "allow"｜"deny"}`. **Single-use, idempotent** — a late/duplicate answer to a resolved or timed-out request is discarded, not replayed. (design §10.4) |
| `GET  /health` | Liveness + status | `{status, auth_ok, queue_depth, oldest_pending_permission_age}`. Used by watchdog + dead-man's switch. |

### 3.2 Push payloads (server→device via APNs)

Opaque, minimal, content-free: `{type, ref}` where `type ∈ {turn_ready,
permission_request, reauth_needed}` and `ref` is the relevant id. The device wakes
and calls `/v1/pending` or `/v1/turn/...` to fetch the actual content over the
tailnet. Gmail/calendar/file content never transits Apple's infrastructure.
*(rationale: design §10.3)*

### 3.3 Internal API (loopback only — the hook talks to this, never on the tailnet)

| Method & path | Purpose |
|---|---|
| `POST /internal/permission-request` | Called by `hooks/pretooluse.py`. Body `{tool, tool_input, session_id}`. **Long-polls** (blocks) up to `T_hook` seconds, then returns `{decision}`. Gate #1 found no sub-90s Claude-side cap, so `T_hook` is *our* choice — the hook self-bounds (e.g. `curl --max-time 55`) and fails closed; pick T_hook for comfortable phone-answer time (~60s) and keep the hook exiting promptly so it never stalls the invocation. |

---

## 4. The permission round-trip (contract confirmed — see gate1-results §2–4)

The one flow that ties hook, queue, push, and phone together. *(rationale: design §3.1;
mechanics: `marlow-v2-gate1-results.md`)*

**Config rule from gate #1:** the tools that should reach the phone must be left off
`permissions.allow`/`ask`/`deny` entirely so they **fall through to the hook** — a
tool listed in `ask` auto-denies in headless and overrides a hook `allow`. `ask` is
reserved for the cron never-prompt policy, not this flow.

1. Attended session hits a **fall-through** (hook-gated) tool. `PreToolUse` hook fires
   (`hooks/pretooluse.py`).
2. Hook POSTs to loopback `/internal/permission-request` and blocks.
3. `permissions.py` mints a `request_id`, registers an `asyncio.Future`, and
   dispatches an APNs `permission_request` wake to the paired device(s).
4. Device wakes, pulls the request from `/v1/pending`, shows Mike *which* action
   it is, he answers → `POST /v1/permission/{request_id}`.
5. Server resolves the Future; the blocked `/internal` call returns the decision;
   the hook emits `hookSpecificOutput.permissionDecision`.
6. **On timeout** (no answer within `T_hook`): server resolves the Future as
   **deny**, the hook denies (fail-closed), **and** the server posts a message
   back into the chat — *"I wanted to do X, couldn't reach you in time, skipped
   it"* — so nothing hangs and nothing happens silently.

Design rules baked in:
- **Decisions are single-use.** If the hook already timed out and Mike answers
  late, the answer is discarded (the request_id is resolved). No replay.
- **The queue prevents colliding decisions** — only one invocation is in flight,
  so at most one permission request is open at a time. (design §3.2)
- **Keep the `ask` set tiny** — every ask is a potential `T_hook` stall ending in
  denial. Reserve it for human-*intent* calls (e.g. Gmail `get_thread`), never for
  blast-radius safety, which belongs in §5. (design §3.1)

**Unattended (cron) never enters this flow.** Cron runs a strict allow-list and
*never* routes a question anywhere; anything not pre-allowed simply doesn't happen
and is surfaced in the briefing/digest ("skipped X — needs approval"). Blocking a
2am job on a sleeping human is never correct. (design §3.1 unattended)

---

## 5. Security boundaries — build these "OpenShell-shaped" *(rationale: design §9.2)*

Declare each boundary in `boundaries.py` as data, enforced standalone now, so a
future NemoClaw/OpenShell policy is *generated from* it rather than a parallel
system that can drift.

**5.1 Filesystem (the §3.6 layered model — this is the concrete spec):**
Express as explicit path-sets, not command-name allow-lists.
- **allow** — `Read`/`Glob`/`Grep` and read-only Bash over the assistant's working
  tree (`~/marlow-identity` and its subdirs).
- **deny** (above allow, wins) — credential/secret paths: `~/.claude/.credentials*`,
  `~/.ssh`, any `.env`, token stores. A "read-only" command reading these is the
  exfiltration risk, since this agent also holds network + messaging tools.
- **argument-shape gating** — deny `find … -exec` / `-delete` (arbitrary exec/delete
  behind a "read-only" name); deny writes/edits outside the working tree; deny
  destructive ops (`rm`, `git push --force`).
- Implemented as settings.json `permissions.allow`/`deny` (prefix matches) **plus**
  the hook for anything needing argument inspection, since settings.json prefix
  matches aren't path-aware. Back it standalone with the systemd unit's restricted
  Unix user. Later this maps 1:1 onto an OpenShell landlock block.

**5.2 Egress:** an explicit host allowlist as a first-class artifact — Anthropic
(API + auth), APNs, Google APIs (Gmail/Calendar), Tailscale, any package registry
needed. Enforce standalone with `deploy/egress.nft` bound to the server's user, not
merely documented. This *is* OpenShell's per-host network policy, pre-built.

**5.3 Secrets:** injected via env, never on disk in the working tree, never in logs
(`log.py` redacts). Mirrors OpenShell `resolve:env` so the later credential-proxy
migration is a swap.

**5.4 First pass ships now, independent of everything else:** run the
`fewer-permission-prompts` skill and implement 5.1 against the *current* interactive
`~/.claude/settings.json`. It de-risks headless mode and improves today's usage
regardless of when the rest lands. This is gate #2 and has no dependency on gate #1.

---

## 6. Liveness & auth lifecycle

**Liveness** *(design §10.2):* systemd `Restart=always` (process death) + a
health-probe watchdog that relaunches on wedge (self-safe, so it can't kill itself)
+ an **independent dead-man's switch on a different host** that curls `/health` and
texts Mike after > N hours down (must be off-box — the in-box alert path dies with
the server).

**Auth lifecycle** ⟨needs GATE#1⟩ *(design §3.7):* `auth_monitor.py` must detect the
expired-token signature and, on detection: (a) **stop draining the queue** against a
dead token — do not let a night of crons "fail" and read like real refusals;
(b) push Mike a `reauth_needed` wake ("SSH in and run `claude`"); (c) hold pending
work where safe. If the credential exposes an expiry, pre-warn in the morning
briefing before it lapses. Re-auth itself is a manual interactive SSH step Mike is
fine doing (~biweekly).

---

## 7. Build order (gates) — **bold = Mike does this by hand**

> **This section is the original plan, not a status board.** It is deliberately
> not annotated with what is done — status lives in exactly two places, so it
> can only drift in one: `marlow-v2-START.md` ("Where things stand") and
> the Status line of each `docs/tasks/*.md`. Read this for *what was intended
> and why*; read those for *what is true*.
>
> Two known divergences from what follows: **gate 6 was reduced** — on-device
> whisper.cpp STT, macOS, and streaming were all cut (see START's P3 entry and
> `docs/tasks/ios-minimal-client.md`), so the gate-6 bullet below overstates the
> scope actually being built. And gates 3–5 have largely shipped as the
> "proactive Marlow" P1–P3 queue rather than in the order written here.

0. **Doc ratification** — done; two items still open (§8 below), neither blocks.
1. **GATE #1 — CLI contract spike (Mike + you, at a terminal).** §1 above. Produce
   `GATE1-RESULTS.md`. Pin the confirmed `claude` CLI version. *Nothing that
   depends on `⟨needs GATE#1⟩` gets built before this.*
2. **Permission audit (independent, ships value now).** §5.4 — `fewer-permission-prompts`
   + the §5.1 layered model on the current interactive setup.
3. **Server skeleton + ingress + liveness.** §2 modules: queue, invoke, app,
   health, boundaries, log; §6 liveness; §3.1 pairing + tailnet. **Mike:** approve
   the first device pairing by hand; confirm the dead-man's switch actually texts
   when you `kill` the server.
4. **Permission round-trip end-to-end.** §4, using GATE#1 facts. **Mike:** trigger
   a real `ask`, answer it on-device; then *ignore* one and confirm the graceful
   "skipped it" path.
5. **Cron cutover.** Move nightly review / weekly digest / morning briefing /
   fitness reminders / monthly check-in onto `cron/entrypoint.py` under the strict
   never-prompt policy. **Scheduling constraint (2026-07-25, gate1-results §8b):**
   each cron invocation re-injects the identity block, and the prompt cache
   expires between widely-spaced turns — so v1's habit of spreading morning
   tasks an hour apart was close to worst-case for caching while buying nothing
   the user could perceive. Prefer **fewer, coarser-grained jobs, or several
   inside one short window**, spaced inside the **240-second dependable cache
   window** measured 2026-07-25 (gate1-results §8b — 240s is the longest gap
   measured to share the cached block; 300s was never sampled and 420s missed,
   so anything beyond 240s is opportunistic and must not be planned around.
   Confirm a larger number before fixing a schedule to it).
6. **iOS/macOS app (SwiftUI multiplatform).** Chat UI, push handling, on-device
   whisper.cpp STT, permission-approval affordance. **Mike:** on-device STT latency
   check; end-to-end voice→Marlow→push round-trip.
7. **Parallel manual track (Mike, anytime — long lead):** Apple Developer account +
   APNs certificate. Gates #4 and #6.
8. **Discord decision.** Keep as fallback or retire — decide once the iOS path is
   proven, not before.

---

## 8. Still open (need Mike; none block starting)

Both items originally here are now decided:
- **First-device pairing bootstrap UI** — resolved in gate #3: an admin-issued
  out-of-band code via a loopback-only endpoint
  (`POST /internal/admin/invite`, was `pairing-code`). See `docs/marlow-v2-gate3-results.md`.
- **Discord's fate** — resolved 2026-07-18: Marlow v1 (tmux + Discord) gets
  **retired** once v2 (this server + the iOS app) is proven out — not kept as
  a permanent fallback. See `deploy/README.md` §0 for what that means for the
  deploy architecture (standalone `marlow` user, own checkout, own auth —
  a seeded-then-diverging copy, not a shared instance with `mike`).

---

## 9. Known accepted risks (don't re-flag as blockers)

- **CLI-contract dependency.** The server parses undocumented headless behavior.
  Mitigations already in the plan: pin the CLI version, a smoke test that re-asserts
  the auto-deny + JSON contract on every CLI bump, and all invocation isolated in
  `invoke.py`. *(design §7)*
- **Terms: settled, not a risk.** The unmodified CLI on the user's own
  subscription is the case the Claude Code legal page allows; the design
  principles that keep it so are in *design §7*.
