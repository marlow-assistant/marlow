# Marlow v2 — Design Doc

> **⚠️ IMPLEMENTERS: build from `marlow-v2-implementation-plan.md`, not this doc.**
> This is the *rationale record*, kept deliberately noisy — it preserves
> superseded options, "leaning toward" framings, and `[R1]`/`[R2]` correction
> trails so the *why* behind each decision is auditable. Read as build
> instructions it will mislead you (you'll hit the discarded answer before the
> final one). Come here **only** when the implementation plan points you here to
> understand why a decision was made. The plan is self-contained; this is not.

**Status:** Decisions final (2026-07-16). Superseded by the implementation plan
for building; retained as the rationale/decision record.
**Author context:** Mike + Marlow (Claude Code), 2026-07-06; Opus adversarial
review + Mike decisions folded in 2026-07-16 (R1 + R2 passes).

> **2026-07-16 review pass.** Opus ran the adversarial review in
> `marlow-v2-opus-review-brief.md` and verified §9 against the NemoClaw
> source firsthand (manifests, policy files, control-plane TS). Mike triaged
> the findings the same day. Changes from that pass are marked
> **[R1 2026-07-16]** inline. The biggest structural additions: server
> ingress auth + liveness were previously unaddressed and now live in the new
> §10; the permission model (§3.1) is now split by session source; §9's
> NemoClaw read is corrected against source.

---

## 1. Goal

Replace the current architecture (Claude Code running interactively inside a
long-lived tmux session, driven via Discord and Remote Control) with:

- Claude Code still doing all the actual work — same skills, same CLAUDE.md,
  same memory system, same subscription (Claude Pro, not API billing).
- A new **always-on server component** that mediates between a **custom
  iOS app** and headless Claude Code invocations (`claude -p`).
- The iOS app reproduces the useful parts of today's Discord UX (chat-style
  interface, push notifications) and adds:
  - On-device speech-to-text via whisper.cpp (better than stock iOS
    dictation, runs well on A15/iPhone 14 Neural Engine, <500ms for
    tiny/base models).
  - Tighter, lower-latency interaction than Discord round-trips.

**Why this over the original voice-pipeline concept (STT → Claude API →
TTS → smart speaker):** that approach paid per-token API costs and rebuilt
orchestration from scratch. This approach keeps Claude Code as the brain
(Pro subscription, all existing skills/memory/behavior) and only replaces
the transport/UX layer.

**Why not keep scraping tmux:** headless mode (`claude -p`, `--continue`,
`--resume`) gives structured, scriptable invocation instead of parsing a
live terminal — no ANSI/alt-screen scraping, no guessing at turn
boundaries. Confirmed via headless-mode docs (2026-07-06) that this
supports the interaction pattern needed here.

---

## 2. Architecture Overview

```
iOS/macOS SwiftUI App            Server Component (Python)         Claude Code
─────────────────────            ─────────────────────────         ───────────
Chat UI            <───push───   Always-on process        
Push notifications                (Linux, same host                 
On-device STT       ───msg───>    Marlow currently runs on)  ──>   claude -p "..."
(whisper.cpp + CoreML)            - receives messages              --continue / --resume
                                  - decides session routing         (headless invocation)
                                  - invokes claude -p
                                  - captures output
                                  - detailed logging       <──     stdout / session file
                    <───push───   - pushes result back to app
```

Windows/Android clients, if ever wanted, are separate thin clients speaking
the same server protocol later (§8) — not part of this build.

Cron jobs (today: nightly review, weekly digest, morning briefing, fitness
reminders, monthly check-ins) invoke the **server component** with a prompt,
instead of injecting into a tmux pane. The server component is now the
single entry point for all Claude Code invocations, whether from iOS, cron,
or anything else added later.

---

## 3. Open Design Questions (must resolve before/during build)

### 3.1 Permission prompts — the hard problem

Interactive Claude Code handles tool-permission prompts and
`AskUserQuestion` via the TUI. Headless mode has no TUI, and confirmed by
testing (2026-07-06, `claude -p` against an out-of-scope Bash command):
**any tool call that would normally prompt is auto-denied, not queued.**
The CLI reports this in two places — a top-level `"permission_denials"`
array in `--output-format json`, and Claude's own response text as it
reacts to the denial. There is no built-in "hold and wait" behavior in
the `-p` CLI path.

**The CLI has no callback hook for this; the Python/TS Agent SDK does**
(`canUseTool` — see §7 for why we're not using the SDK anyway). SDK
`canUseTool` pauses execution indefinitely and lets your code return
allow/deny/modify, which is exactly the round-trip-to-phone behavior we
want — but it's not CLI-exclusive machinery, just more turnkey there.
The CLI path can get the same effect via a **`PreToolUse` hook** in
settings.json: the hook shells out to the server component and blocks
until it gets an answer back, then returns an allow/deny decision via
the hook's `hookSpecificOutput.permissionDecision`. This works for both
CLI and SDK since hooks are a shared mechanism — a `PreToolUse` hook script
is the concrete plan for routing permission decisions to the phone).

- **(a) Permissive headless mode.** Run with a permissive
  `permissions.defaultMode` (e.g. accept-edits or bypass) and rely on the
  existing allow/ask/deny rules in settings.json to do the real gating for
  anything genuinely risky. Simpler, but "ask" rules (e.g. the Gmail
  `get_thread` rule added today) have nothing to prompt *to* — confirmed
  they just auto-deny in this mode, same as everything else that isn't on
  the allow list.
- **(b) `PreToolUse` hook round-trips to the phone.** Hook intercepts
  before Claude Code's own permission evaluation would auto-deny, calls
  out to the server component (blocking), server pushes the question to
  iOS, waits for a reply, hook returns the decision. More faithful to
  current behavior, but is a real sub-project: needs a defined message
  schema for "this turn is blocked on a decision," a UI affordance in the
  Swift app for responding, and the hook needs a way to block on that
  response (sync HTTP call from the hook script to the server works;
  server manages the actual wait/timeout logic).

**Recommendation:** decide this first — it shapes the request/response
contract between server and phone. Leaning toward (b) for anything in the
`ask` list, since that list was just deliberately populated (Gmail
`get_thread`) specifically to require human confirmation. Everything else
(see §3.6 below) should be handled by widening the `allow` list, not by
routing more things through (b) — the phone round-trip should be reserved
for genuinely sensitive actions, not routine noise.

**[R1 2026-07-16] DECIDED — split the permission policy by session source,
with a bounded (not indefinite) wait.** Two corrections from the review
supersede the "lean toward (b)" framing above:

1. **A PreToolUse hook cannot block indefinitely.** The SDK's `canUseTool`
   genuinely suspends the turn until your code answers; a hook runs under a
   bounded timeout (Claude Code default ~60s, configurable). Phone asleep or
   Mike mid-meeting → hook exceeds its timeout → killed with no decision →
   headless falls through to auto-deny. So (b) does **not** deliver "hold the
   turn and wait for the human" — it delivers "wait up to the hook timeout,
   then fail closed." §7's "hooks achieve the same as `canUseTool`" is
   therefore too strong and is corrected there. This must be verified against
   the installed CLI version before any server code — it's build gate #1
   (§11).

2. **Unattended and attended sessions want opposite defaults.** The doc
   previously applied one permission policy to both cron and iOS. They differ:

   - **Unattended (cron / scheduled):** strict allow-list, **never routes a
     question to the phone.** Anything not pre-allowed simply does not happen
     and is logged; the surrounding briefing/digest surfaces it ("skipped X —
     needs approval"). Blocking a nightly 11:55pm job on a sleeping human is
     never correct.
   - **Attended (iOS chat):** the (b) hook round-trip, but explicitly
     **bounded**. On timeout: fail closed (deny) **and** post a message back
     into the chat — "I wanted to do X, couldn't reach you in time, skipped it"
     — so nothing hangs and nothing happens silently. Mike's framing: if
     Marlow is waiting on a tool permission and gets no answer in ~60s, the
     tool is denied and the agent has to reason about whether/how to continue.

> **[R3 2026-07-16] GATE #1 ran — this model is confirmed, with one correction.**
> Verified on CLI 2.1.211 (see `marlow-v2-gate1-results.md`). A PreToolUse hook
> **can grant** in headless `-p` (round-trip validated) — but the round-trip is
> driven by tools **falling through to the hook**, NOT by the `permissions.ask`
> list. An `ask` entry auto-denies in headless and *overrides* a hook `allow`, so
> `ask` is the **cron auto-deny layer**, not the phone mechanism. Also: there is
> **no sub-90s wait cap** (a 90s hook was honored), so the bound is set by the
> hook's own self-timeout; and a denial reports `is_error=false`/`exit=0`, so the
> server must detect it via a non-empty `permission_denials` array, not exit code.

3. **Design goal that falls out of this: keep the `ask` set small.** Because
   every `ask` is a potential 60s stall that ends in denial, the permission
   model should be built so genuine phone round-trips are *rare* — reserved
   for the human-*intent* category (the Gmail `get_thread` privacy call), not
   for blast-radius safety. Blast-radius safety belongs in the allow/deny-path
   layering (§3.6) and, as a bonus layer, OpenShell (§9.1) — not in an
   interactive prompt. A permission model with few asks degrades gracefully;
   one with many asks stalls constantly when Mike is unreachable.

### 3.6 Permission noise — need much wider default allows

**Problem, observed in current (tmux/interactive) usage, not just a
headless concern:** Mike gets prompted constantly for things that should
be uncontroversial — `cat`, `grep`, `ls`, reading files the assistant's
Linux user already has read access to. This is too restrictive today and
will be actively unworkable in headless mode, where every prompt either
needs a phone round-trip (§3.1b, slow, meant for genuinely sensitive
stuff) or silently auto-denies (§3.1a, breaks things Mike actually wants
to happen).

**Direction (not wide open, but wider than today):**
Marlow runs as a specific Linux user (see host/systemd setup) — Unix file
permissions are already a real access-control boundary. There's little
value in Claude Code re-prompting for reads/greps/lists that the OS would
happily allow any process running as that user to do anyway. Read-only,
side-effect-free commands (`cat`, `ls`, `grep`, `find`, `head`, `tail`,
`stat`, etc.) and the `Read`/`Glob`/`Grep` tools generally should be broad
allow-list entries, not per-invocation prompts.

**What should stay gated:** destructive/irreversible operations (`rm`,
`git push --force`, editing outside the assistant's own directories),
anything touching credentials/secrets, and the specific things already
deliberately placed in `ask` (e.g. Gmail `get_thread` — a human-privacy
call, not a Linux-permissions call). The Linux user's *write* access
already limits blast radius for anything actually dangerous within its
own home directory — the remaining risk surface is things like external
side effects (sending email, posting somewhere, spending money) rather
than local reads.

**Needs doing (either now, independent of Marlow v2, or as part of the
build):** audit current `~/.claude/settings.json` and project settings,
and broaden `permissions.allow` with prefix rules for common read-only
Bash commands and the Read/Glob/Grep tools, rather than leaving them to
fall through to default-ask. The `fewer-permission-prompts` skill exists
for exactly this — worth running it as a first pass before or alongside
the Marlow v2 build, since it'll help the *current* interactive setup too,
not just the future headless one.

**[R1 2026-07-16] CORRECTION — "read-only" is not the same as "safe";
allow-list by path, not just by command name.** The review found the
command list above is unsafe as originally framed, in ways that defeat this
section's own "credentials stay gated" goal:

- **Command name ≠ behavior.** `find … -exec` / `-delete` is arbitrary
  execution/deletion behind an allow-listed "read-only" name. Allow-listing
  must gate on *argument shape*, not just the command (`find` alone is not
  safe to blanket-allow).
- **Disclosure is the exploit for a networked agent.** `grep -r` / `cat` /
  `tail` over `$HOME` reads `~/.ssh/id_*`, `.env`, and specifically
  `~/.claude/.credentials.json` — the Pro-subscription token this whole
  architecture depends on. For an agent that also holds network + messaging
  tools, reading a secret puts it in the transcript, the server log (§3.4),
  and one allowed `curl`/message away from exfiltration. "Read-only" says
  nothing about *what* is read.
- **settings.json allows are prefix string matches, not path-aware
  sandboxing** — so "read-only but never `~/.ssh` or the credential store"
  can't be expressed with allows alone.

**Revised direction:** layered model, not a flat widened allow-list —
  (1) broad read *allow* scoped to the assistant's working tree,
  (2) an explicit **deny** list above it for credential/secret paths
      (`~/.claude/.credentials*`, `~/.ssh`, `.env`, token stores),
  (3) argument-shape gating for the dual-use commands (deny `find … -exec`).

**Principle (Mike, 2026-07-16): do the right thing standalone regardless of
OpenShell.** This layering has to be correct on its own on any host.
OpenShell (§9.1) is an *additional* guard against Marlow making a mistake —
and it's the layer that can enforce "read-only but not `~/.ssh`" cleanly
where settings.json can't (confirmed against NemoClaw's policy schema, §9) —
but the standalone deny-layer is built and correct first, not deferred to the
sandbox.

### 3.2 Session / concurrency model

Multiple triggers will want to talk to "the same" Marlow conversation:
cron jobs, iOS-initiated messages, possibly future triggers. Options:

- **One shared session ID** for everything, using `--resume`. Simple, but a
  cron job firing mid-conversation with the phone could collide or
  interleave confusingly.
- **Separate sessions per trigger source** (e.g. one for iOS chat, one for
  cron/scheduled), with the server responsible for merging/summarizing
  context across them if needed (similar to how memory/*.md already
  carries cross-session context today).

**Recommendation:** start with separate sessions per source + shared
memory files as the cross-session context bridge (this is already how the
system works today — memory/*.md, not tmux state, is the actual source of
truth for continuity). Revisit only if that proves insufficient.

**[R1 2026-07-16] DECIDED — the server serializes all `claude -p`
invocations through a single work queue (Mike).** "Separate sessions +
memory files as the bridge" left a real write-race unaddressed: cron and
iOS can invoke `claude -p` concurrently, and CLAUDE.md's write rule ends
every memory write with `git add -A && commit && push`. Two concurrent
invocations → racing commits → non-fast-forward push rejection or a silently
clobbered edit (e.g. the nightly 11:55pm review fires while Mike messages
from a different timezone; one session's memory update is lost with only a
stderr nobody reads). Fix, per Mike: the persistent server component
**mediates** all `claude -p` invocations — one in-flight session-write at a
time, everything else queued. This confirmed direction also resolves the
§3.1 concurrency-of-decisions problem (no two blocking permission hooks
racing to the same phone) and is the natural place to assign the request IDs
that make permission round-trips idempotent. Memory files remain the
cross-session *context* bridge; they are explicitly **not** the
synchronization mechanism — the queue is.

*Still open (minor, non-blocking):* staleness window — the iOS user won't
see a cron-originated action until the next memory read. Acceptable for now;
revisit only if it causes real confusion.

### 3.3 Audio handling

iOS app does STT on-device (whisper.cpp) rather than sending raw audio to
the server for transcription — decided 2026-07-06. Server does **not**
need to run faster-whisper on incoming audio; it receives already-
transcribed text from the phone. (Today's Discord flow does the reverse —
audio attachment downloaded and transcribed server-side via
`scripts/transcribe.py` — that logic becomes unnecessary for the iOS path
specifically, though may still be needed if Discord remains a secondary
channel.)

### 3.4 Debugging model

Replaces "attach to tmux and watch the conversation" with "read the
server component's detailed log." Log needs to capture, at minimum:
- Incoming message (source: iOS/cron/other, session ID, raw content)
- The exact `claude -p` invocation (flags, session/resume ID used)
- Full stdout/response
- Any error/non-zero exit
- Push notification sent (or failed) as a result

### 3.5 Self-awareness / self-modification

Marlow v2 needs skills/config describing **its own server component** —
mirroring how the current system already documents its tmux/systemd setup
(CLAUDE.md, marlow.service, marlow-v2-checkin.sh) so Marlow can reason
about and modify its own scheduled tasks. This design doc + a follow-up
"how the server component works" reference doc should together fill that
role once built.

### 3.7 Auth token lifecycle [R2 2026-07-16]

`claude -p` reuses interactive Claude Code's stored OAuth credential
(`~/.claude/.credentials.json`), refreshed via the paste-an-OAuth-URL flow
that only works interactively. Refresh is infrequent (Mike's estimate: on the
order of every couple of weeks) and Mike is fine SSHing to the host to re-auth
by hand when it's needed. But the always-on server MUST treat token expiry as
a first-class, human-in-the-loop state, not a generic failure:

- **Detect; don't silently burn the queue.** An expired/invalid token makes
  *every* `claude -p` fail. The server has to recognise the auth-expired
  signature and stop dispatching queued work against a dead token — otherwise
  a night of crons all "fail," and, worse, the failures can read like genuine
  tool refusals rather than "nobody's logged in."
- **Surface interactively.** On auth-expired: push Mike a clear "re-auth needed
  — SSH in and run `claude`" message, and hold/queue pending work rather than
  discard it where that's safe.
- **Pre-warn if the credential exposes an expiry.** Warn from a morning-briefing
  line *before* the token lapses rather than discovering it at 2am. Whether the
  stored credential carries a readable expiry is a gate-#1 question (§11).
- **Learn the failure signature in gate #1.** The exact exit code / stderr /
  JSON shape `claude -p` emits on an expired token, so the server can tell
  "needs human re-auth" apart from a transient error.

> **[R3 2026-07-16] GATE #1 findings (see `marlow-v2-gate1-results.md`).** It's a
> **two-token** OAuth: `accessToken` (~8h) auto-refreshes headlessly and writes
> back to `.credentials.json`; `refreshToken` (~9d) is what needs the interactive
> paste. Watch **`refreshTokenExpiresAt`**, not `expiresAt`. The refresh window
> appears **anchored, not rolling** (one sample) → ~weekly interactive re-auth,
> matching Mike's tolerance; confirm over the coming week. macOS stores the token
> in Keychain, Linux in the file. The expired-*refresh*-token failure signature is
> the one gate-#1 sub-item still to capture.

This is also a NemoClaw-adoption consideration (§9.3): under OpenShell the
periodic *interactive* OAuth refresh needs permitted egress to Anthropic's auth
endpoints, and the credential file either mounts as an injected secret or routes
through `resolve:env`.

---

## 4. What Carries Over Unchanged

- CLAUDE.md behavior rules, SOUL.md identity, memory/ system — all as-is.
- Skills, MCP servers (Gmail, Calendar, Discord, etc.) — unchanged, just
  invoked from headless sessions instead of interactive ones.
- Permission philosophy (allow/ask/deny lists in settings.json) — same
  lists, different enforcement mechanism per §3.1.
- Cron-driven behaviors (nightly review, weekly digest, morning briefing,
  monthly Marlow v2 check-in, fitness reminders) — same schedule and
  prompts, just invoked via the server component instead of tmux inject.

---

## 5. What's New / Needs Building

1. Server component (always-on process, same host Marlow runs on today):
   - Message ingestion from iOS (transport TBD — push notification
     service + some request channel; APNs certificate/setup needed)
   - `claude -p` invocation logic (session routing, `--continue`/
     `--resume` handling)
   - Detailed logging (see §3.4)
   - Push notification dispatch back to iOS
   - Cron entry point (replacing tmux inject)
2. SwiftUI multiplatform app (iOS + macOS, one project — see §8):
   - Chat-style UI (Discord-equivalent)
   - Push notification handling
   - On-device whisper.cpp STT (tiny/base model, already validated fast
     enough on A15, CoreML-accelerated)
   - UI for resolving permission prompts, if §3.1(b) is chosen
3. Migration plan for existing Discord-based flows — decide whether
   Discord stays as a secondary/fallback channel or is fully replaced.
4. Wider default permission allow-list (§3.6) — needed regardless of
   Marlow v2 shipping, but a hard blocker for headless mode specifically
   since there's no interactive fallback once tmux is gone.

---

## 6. Background / Prior Context

See `memory/projects.md` → "Marlow v2 — Voice Assistant" for the earlier
(June 29) voice-pipeline concept (wake word → STT → Claude API → TTS →
smart speaker) and the July 4 note about Discord reliability problems that
first raised "phone as primary UI" as an idea. This doc supersedes that
architecture while keeping the underlying hardware notes (iPhone 14 /
A15 whisper.cpp performance, host machine options) relevant.

---

## 7. CLI (`claude -p`) vs Python/TypeScript Agent SDK — decided: use the CLI

Investigated 2026-07-06 whether the Agent SDK (`claude_agent_sdk` /
`@anthropic-ai/claude-agent-sdk`) would be a better foundation for the
server component than shelling out to `claude -p`. Verified against
official docs (`code.claude.com/docs/en/agent-sdk/overview` and
`.../agent-sdk/user-input`):

**Billing — this is the deciding factor.** The Agent SDK does **not**
support Claude Pro/Max subscription auth. Official setup docs start with
`export ANTHROPIC_API_KEY=...` and include this explicit note: *"Unless
previously approved, Anthropic does not allow third party developers to
offer claude.ai login or rate limits for their products, including agents
built on the Claude Agent SDK. Please use the API key authentication
methods described in this document instead."* This is a stated policy
line, not just a missing feature — building the server component on the
SDK means API-key/token billing, not the Pro subscription. That
contradicts the whole point of this architecture (see §1). The `claude -p`
CLI path is the one confirmed to draw from the subscription. (One search
result mentioned a June 2026 weekly "Agent SDK credit" bundled into
Pro/Max plans — unverified against official docs, treat with suspicion
until confirmed directly.)

**Permission handling — the SDK has the nicer mechanism, but it's not
SDK-exclusive.** The SDK's `canUseTool` callback pauses execution
indefinitely and waits for your code to return an allow/deny/modify
decision — exactly the phone-round-trip behavior described in §3.1(b).
But the underlying mechanism (hooks) is shared with the CLI: a
`PreToolUse` hook can achieve the same result by blocking on a call to the
server component. So the SDK doesn't unlock anything the CLI can't do
here, it's just more turnkey.

> **[R1 2026-07-16] CORRECTION — this equivalence is overstated.** A hook
> blocks only up to Claude Code's hook timeout (~60s default); `canUseTool`
> can suspend indefinitely. So the CLI path gets "bounded wait then fail
> closed," not "hold the turn until the human answers." This doesn't reopen
> the CLI decision (billing still rules the SDK out), but the permission
> model is designed around a *bounded* wait — see §3.1 [R1]. Confirm exact
> timeout/deny semantics on the installed CLI version as build gate #1 (§11).

**[R1 2026-07-16] CLI-behavior dependency risk (add to fallback thinking).**
The server parses undocumented headless behavior — the auto-deny fall-through
and the `permission_denials` JSON shape. A CLI update changing either could
silently mis-route. Mitigations to build in: pin the `claude` CLI version;
add a smoke test that re-asserts the auto-deny + JSON contract on every CLI
bump; and isolate *all* invocation behind one module so an SDK or
API-key-billed path is a swap, not a rewrite, if `claude -p` ever regresses.
**Terms (settled 2026-10-19):** the Claude Code legal page allows the
unmodified CLI on the user's own subscription, including where a platform
hosts it. The design principles that keep Marlow within that hold whether or not the
repo is public:

1. **The unmodified `claude` binary only.** Marlow never reads the OAuth
   token to call the API itself. `auth_monitor.py` reads only the expiry
   timestamps from `.credentials.json`, and nothing from that file leaves
   the box.
2. **Sign-in only through Anthropic's own flow, in the CLI.** No in-app
   login relay that carries the login code through Marlow.
3. **One subscription, one person.** Marlow never serves another person
   from your login. A second user gets their own instance on their own
   login.
4. **The README says so.** Users run Marlow under their own plan, its terms
   and its limits.

A hosted fork is held to the same lines.

**Decision: use the CLI (`claude -p` / `--continue` / `--resume`),
build the permission round-trip via a `PreToolUse` hook rather than
`canUseTool`.** Keeps the Pro subscription, keeps parity with the
existing interactive setup (same settings.json, same hooks mechanism),
and doesn't cost anything in permission-handling capability once the
hook is built.

---

## 8. Client and server tech choices (decided 2026-07-06)

Pressure-tested both. Key framing that resolved both questions: **the
server mediates everything, so clients only need to speak the same
protocol — they don't need to share a codebase with each other or with
the server.** That removes the pre-AI pressure toward "one language
everywhere for human context-switching," since no human needs to hold
both sides in their head — Claude Code does the writing on each side
independently.

### 8.1 Client: SwiftUI multiplatform (iOS + macOS), not a cross-platform framework

Requirements: iOS and Mac daily use (only platforms Mike and his wife
actually have); "would also be good" on Windows; Android/open-source is
speculative/future, not a current requirement.

**Decision: native SwiftUI, one multiplatform project covering iOS +
macOS.** Reasoning:
- Because the server is the actual integration point, Windows and Android
  don't need to share a codebase with the iOS/macOS client — they can be
  independent thin clients built later, whenever (if ever) actually
  wanted, without any compromise to the primary client today.
- whisper.cpp's CoreML acceleration path is native Apple tooling. A
  cross-platform framework (Flutter, React Native) would still need a
  native module to reach it — so going native for the platforms used
  daily costs nothing extra on the STT side and avoids added framework
  risk everywhere else (build tooling, desktop-target maturity, etc.).
- Windows was explicitly a soft want, not a requirement — not worth
  constraining the primary client's quality to accommodate it now.

**Deferred, not rejected:** Windows and Android clients are each a later,
separate build against the same server protocol, if/when actually wanted.

### 8.2 Server: Python

Considered C#/.NET, Python, Go, Node/TypeScript. The deciding
consideration, identified during pressure-testing: since Claude Code
writes and will later self-modify this component (§3.5), the language
that matters most is the one the *agent* iterates on fastest and most
reliably — not a human-maintainability or client-language-matching
argument (neither applies once the client is a separate Swift codebase).

- **Python — decided.** No build step (faster iterate/debug loop for the
  same agent also touching cron/hook logic live); `asyncio.Event`/`Future`
  covers the blocking-wait-for-phone-response pattern (§3.1b) as well as
  any alternative; matches existing repo conventions (`transcribe.py`,
  the whisper venv, CLAUDE.md's own default of Python for
  scripts/automation); deepest training-data familiarity of any
  candidate, which matters directly for how reliably Claude Code can
  write and later modify this code unsupervised.
- **Go — closest technical fit for the concurrency pattern specifically**
  (channels map even more directly onto "block until another goroutine
  delivers the resolved decision" than Python's asyncio), and would ship
  as a single low-footprint static binary. Considered a resource-usage
  edge on the aging Marlow host, but ruled out: this is a long-running
  process, not something spawned per-request, so startup cost is
  irrelevant, and steady-state load is I/O-bound (subprocess management,
  JSON parsing, HTTP calls to APNs) rather than CPU-bound — the workload
  the server actually has isn't performance-sensitive enough for Go's
  edge to outweigh Python's iteration-speed and familiarity advantages.
- **C#/.NET — solid, not chosen.** Genuinely good async primitives
  (`TaskCompletionSource` matches the same pattern) and is Mike's stated
  default for application/server code, but that default exists for
  human-maintainability reasons that don't apply once "Claude Code writes
  and maintains all of it" is taken seriously — and it adds a second
  runtime/toolchain (dotnet SDK) to the host purely for this component.
- **Node/TypeScript — weakest option, no longer has its main selling
  point.** Would have been attractive if the client were also
  JS-based (shared types), but that argument evaporated once the client
  became native Swift.

---

## 9. NemoClaw compatibility — preliminary read, not a final decision (2026-07-16)

Mike raised whether targeting NVIDIA's NemoClaw (open-source agent
sandboxing project, supports OpenClaw and Hermes among others,
control plane ~90% TypeScript/Node.js) as a platform for Marlow v2 should
change the §8.2 language decision. Researched the actual integration
contract rather than relying on marketing copy — findings below are a
first pass, flagged for a second opinion (another AI, or the NemoClaw
team directly) before treating as settled.

**What was found (moderate-to-high confidence, alpha project so treat as
a snapshot):**
- Each supported agent declares an `agents/<name>/manifest.yaml` — this
  is the actual integration contract, not the control plane's TS source.
  Confirmed by reading `agents/openclaw/manifest.yaml` (language: nodejs)
  and `agents/hermes/manifest.yaml` (language: python) directly.
- The contract is process + HTTP, not a TypeScript interface: an
  installable binary, a `gateway run`-style start command, an HTTP
  health-check endpoint, a declared config directory/format, and an
  optional MCP bridge adapter. The control plane spawns the agent as a
  subprocess inside an OpenShell sandbox and talks to it over the
  health-probe port — it does not import agent code or require
  implementing a TS class.
- Hermes (pure Python) satisfies this identically to OpenClaw
  (Node.js) — direct proof the contract is language-agnostic.
- **Caveat on maturity:** the manifest schema is explicitly mid-migration
  ("Phase 3 of the multi-agent plan" not complete per the manifests
  themselves); legacy root-level artifacts are still pending move
  into the per-agent structure. No documented "bring your own agent"
  path exists for outside contributors — adding Marlow as a 4th agent
  would likely mean engaging NVIDIA's roadmap/PR process, not following
  a stable published spec.

**Preliminary conclusion — hold loosely:**
- The technical case for building Marlow's backend in TypeScript to ease
  NemoClaw integration looks like a red herring: Python satisfies the
  manifest contract exactly as well as Node does. §8.2's Python decision
  doesn't need to change on integration grounds.
- Mike's second goal — using this project as a vector to get more
  fluent in Node/TS, given its traction in the agent-tooling community —
  is real but probably better served by occasionally reading/patching
  NemoClaw's TS control plane directly (e.g. the manifest-migration work)
  than by writing Marlow itself in TypeScript. Talking to a Python
  agent's HTTP health endpoint doesn't exercise much Node skill either
  way.
- **This is a preliminary read based on one research pass through public
  repo/docs content (some fetched through summarization rather than raw
  diffs) on an alpha project whose schema is known to be actively
  changing.** Worth revisiting with a second opinion — another AI
  session, or ideally the NemoClaw team — before this is treated as
  final, and before any actual commitment to targeting NemoClaw as a
  platform (which itself isn't yet decided — see open question below).

**Still open:** whether Marlow v2 should target NemoClaw as a platform
at all (vs. the standalone server architecture in §1-§8), independent of
the language question. Not resolved by this research pass.

### 9.2 Corrections from a firsthand source read [R1 2026-07-16]

Opus verified §9 against the actual NemoClaw repo (`agents/*/manifest.yaml`,
`agents/hermes/policy-permissive.yaml`, `schemas/sandbox-policy.schema.json`,
`src/lib/agent/definition-types.ts`, `AGENTS.md`). The §9 read holds in
outline but needs three corrections and one confirmation:

- **"Python satisfies the contract as well as Node" is right for the agent
  *process*, incomplete for the *integration*.** Confirmed: Hermes ships a
  Python binary and satisfies `manifest.yaml` identically to OpenClaw's Node
  gateway. **But** each agent also ships non-trivial TypeScript *inside
  NemoClaw's control plane* — `agents/hermes/generate-config.ts` plus a whole
  `agents/hermes/config/` tree (`hermes-config.ts`, `hermes-env.ts`,
  `host/tool-gateway-broker.ts`, `config/managed-tool-gateway.ts`, …), and
  AGENTS.md (§235–239) confirms adding an agent means writing a manifest *and*
  a per-agent TS config-generation entrypoint. So **adding Marlow as a 4th
  agent is real control-plane TypeScript work regardless of Marlow's own
  backend language.** This flips §9's throwaway conclusion that NemoClaw
  wouldn't exercise Node skill: integration is *exactly* where the Node work
  lives — which serves Mike's Node/TS-fluency goal better than §9 credited,
  and is best pursued via the separate "land a first PR on NemoClaw" track
  (projects.md) rather than by writing Marlow itself in TS.

- **Client-model conflict §9 never surfaced.** NemoClaw agents expose their
  UI/API on `forward_ports` (Hermes: 8642 API + 18789 dashboard) behind
  NemoClaw's browser surface, with device-pairing or bearer auth. That does
  **not** naturally accommodate §8's native iOS app holding its own channel to
  a standalone Python server. Inside OpenShell, egress is default-deny and
  per-host enforced — the APNs round-trip, the iOS↔server channel, *and*
  Anthropic's own auth endpoints (for `claude -p`) would all have to be added
  to the network policy. ~~**Targeting NemoClaw as a deployment platform is in
  genuine tension with the §8 client architecture**~~ — *[R2 2026-07-16
  correction, Mike:] "genuine tension" is overstated, same failure mode as §7's
  overstatement.* `forward_ports` is just a loopback port inside the sandbox;
  how a remote client reaches it is unspecified and orthogonal. Mike's actual
  NemoClaw setup reached the agent's web UI by SSH-tunnelling it
  to his laptop — Tailscale is the same *class* of thing, a different tunnel.
  So there is **no architectural conflict** between the §8 iOS-client model and
  NemoClaw, only an unspecified remote-access transport that Tailscale validly
  fills. The real content is an adoption *checklist* (egress for iOS↔server,
  APNs, Anthropic auth; inbound access to the forward_port), not a conflict —
  see the new §9.3. (Speculative roadmap dependency, NVIDIA's to build:
  OpenShell already does L7 egress filtering with credential rewrite, so an
  inbound L7 filter in front of the forward_port — a WAF for the agent — is a
  plausible future, but not something Marlow designs around now.)

- **Credential model §9 missed.** NemoClaw injects secrets via an L7 proxy
  (`resolve:env`, `request_body_credential_rewrite` in the policy files), not
  sandbox-stored keys. Marlow's Gmail/Calendar OAuth **and the Claude Pro
  token** would have to route through that, and Anthropic's endpoints be an
  allowed egress host, or `claude -p` inside OpenShell can't authenticate and
  is dead on arrival. Solvable, but real integration work invisible in §9.

- **Confirmation — §9.1's granularity question is now answered, and §9.1's
  conclusion holds.** The policy schema *is* granular: per-host / per-method /
  per-path network rules with credential rewrite, landlock read-only/read-write
  filesystem paths, syscall-level containment. So OpenShell can cleanly enforce
  the "read-only but never `~/.ssh`" guard that settings.json can't (this is
  why it's a real bonus layer for §3.6). But it operates at
  network/fs/syscall layers, **not** tool semantics — it structurally cannot
  express "allow Gmail `get_thread` except ask on this thread." That is exactly
  what §9.1 concluded, now confirmed against source: sandbox collapses
  read-only blast-radius noise; the human-*intent* asks still need the §3.1
  round-trip. The "two permission systems drift" worry is milder than feared —
  the layers are *complementary* (different layers), not duplicative, so the
  risk is *gaps between them*, not divergence.

**Bottom line — [R2 2026-07-16] DECIDED (Mike):** ~~keep NemoClaw a backlog
spike~~ → **build Marlow v2 standalone per §1–§8, with every security boundary
declared the way OpenShell would enforce it, and go in eyes-open to what a
future move behind NemoClaw will require (§9.3).** This supersedes R1's flat
"standalone, NemoClaw is a backlog spike," which treated the two as orthogonal.
The synthesis Mike ratified: not "build crappy and let OpenShell clean up" (the
false choice), and not "build on NemoClaw from day one for free hardening"
(analysed and rejected below) — but *build the boundaries ourselves, shaped so
OpenShell later enforces in hardware what we already declared in software.*

Why not build on NemoClaw from the start (the streamlining question, answered
no): OpenShell's hardening (landlock fs, per-host egress, credential proxy,
syscall containment) can't be cherry-picked — it comes bundled with the whole
deployment model (Dockerfiles, `start.sh`, socat forwarding) and an alpha,
mid-migration schema with **no "bring your own agent" path for outside
contributors** (the manifests say Phase 3 isn't done). Adopting it now
makes the assistant Mike depends on *daily* boot-dependent on someone else's
moving target and front-loads the per-agent control-plane TypeScript as a
blocker. Net: more effort, more fragility, later ship — not less.

What "OpenShell-shaped standalone" means concretely (build these now, so
adoption is policy-mapping not rewrite):
- **Filesystem:** express §3.6 as explicit read-only / read-write *path sets*,
  declaratively — a landlock policy written in software. Standalone, back it
  with the systemd unit's restricted user; later it maps 1:1 onto OpenShell's
  landlock block.
- **Egress:** maintain an explicit host allowlist (Anthropic, APNs, Google
  APIs, Tailscale) as a first-class config artifact, enforced standalone with
  nftables on the server's user — not merely "documented." That *is* OpenShell's
  per-host network policy, pre-built.
- **Secrets:** injected via env, never on disk in the working tree, never in
  logs (§10.3) — mirrors `resolve:env`, so the credential-proxy migration is a
  swap, not a redesign.
- **Config generation** kept as a concern separate from the agent runtime, so a
  future `generate-config.ts` has a clean surface to target.

Payoff: this also kills the "two permission systems drift" worry outright — the
standalone declarations are the *source of truth*, and any future OpenShell
policy is generated to match them, so they can't diverge. The Node/TS-fluency
goal still rides the separate NemoClaw-PR track (projects.md), not Marlow's own
backend language.

### 9.3 NemoClaw ingress model & the three deployment shapes [R2 2026-07-16]

R1's §9.2 asserted a client-model conflict without ever documenting how a
NemoClaw agent actually talks to the outside world. Read from the manifests
directly, there are **two distinct channels**:

1. **Inbound HTTP the agent serves, exposed via `forward_ports`.** Hermes
   serves `:8642` (OpenAI-compatible API + `/health`) and `:18789` (dashboard);
   OpenClaw serves `:18789`. These are loopback ports inside the sandbox that
   NemoClaw bridges out (socat) to be reached however the operator tunnels in —
   SSH tunnel today, plausibly Tailscale later. Inbound auth is exactly the two
   models in the manifests: OpenClaw `device_pairing`, Hermes `bearer_token`
   (`API_SERVER_KEY`). **This is the "accept a connection" infrastructure — it
   exists**; the earlier worry that it might not is put to rest.
2. **Outbound messaging bridges** — Telegram, WhatsApp, WeChat/iLink. The agent
   connects *out* to a third-party message bus and long-polls; the human's phone
   talks to that platform, never to the sandbox (Hermes stores
   `platforms/whatsapp/session`, pairs users with `hermes pairing approve`).

So Marlow has three possible deployment shapes; we are building shape 1 with an
eye to shape 2:

- **Shape 1 — Standalone (§1–8, chosen).** iOS → Tailscale → Python server on
  its own host, no sandbox. Full control of ingress; §10.1 could just decide it.
- **Shape 2 — NemoClaw, inbound iOS (deferred, not foreclosed).** iOS →
  Tailscale → a `forward_ports` endpoint into OpenShell. The bearer/pairing
  machinery fits the §10.1 device-token plan cleanly. Adoption checklist (the
  eyes-open list): egress policy must include Anthropic auth + APNs + Google
  APIs + Tailscale; inbound access to the forward_port must be permitted (maybe
  a future OpenShell inbound WAF); the Pro credential mounts as an injected
  secret / routes through `resolve:env`, and the periodic *interactive* OAuth
  refresh (§3.7) needs permitted Anthropic egress; and adding Marlow as an agent
  means writing the per-agent control-plane TypeScript (§9.2). None of these are
  architectural blockers — they're work, visible in advance.
- **Shape 3 — NemoClaw, messaging transport (ruled out on sight, named for
  completeness).** Drop the custom app; bridge through Telegram/WhatsApp, the
  way a supported messaging agent does. No inbound problem at all — but it discards the entire reason for
  Marlow v2 (latency, on-device whisper.cpp, tailnet privacy) and re-inherits
  the messaging-reliability failure mode that started this project.

### 9.4 Egress, and why OpenShell's credential proxy matters now [2026-10-16]

Two things this section didn't anticipate. A second instance on a raw API key
(`docs/tasks/marlow-for-taylor.md`) puts the key in the CLI's env, where the
agent's shell reads it (probed, Q5). OpenShell's credential proxy is the clean
fix, because the key would never enter the sandbox. And egress enforcement,
built standalone, would be throwaway if Marlow moves under OpenShell soon, so
it waits on a re-check of NemoClaw (Mike, from about 2026-10-30). The
comparison and what the re-check must answer: `docs/tasks/egress-and-webfetch.md`.

### 9.1 Does OpenShell change the §3.1/§3.6 permission calculus? (tentative)

Raised 2026-07-16: if Marlow actually ran inside OpenShell's sandbox
(not just manifest-compatible with NemoClaw, but deployed on it), the
containment is reportedly hardware-enforced network/filesystem/syscall
policy — stronger than the "Marlow runs as a specific Linux user"
argument in §3.6. That could justify collapsing more of the read-heavy
default-ask noise into default-allow, since the sandbox, not the
allow-list, would be doing the actual blast-radius containment.

**Resolved principle (not tentative): this must be defense-in-depth, not
a dependency.** It would be irresponsible to make Marlow's safety model
depend on running inside NemoClaw specifically — that makes the agent
unsafe-by-default on any other host (current host, future non-NemoClaw
box, local dev while building it), and §3.6's Unix-permission-based
allow/ask/deny design has to work standalone regardless. OpenShell, if
adopted, tightens the blast radius further as a bonus layer — it doesn't
replace the baseline. Practical effect: the §3.6 allow-list audit
(`fewer-permission-prompts` skill pass) is fully valid and non-blocking
work regardless of how the NemoClaw platform question resolves, and
should proceed on that basis.

**Also doesn't fully apply:** sandbox containment addresses blast
radius/system safety, not privacy/intent. The Gmail `get_thread` `ask`
rule exists because of what Marlow *should* read, not what it *could*
damage — OpenShell can't make that call. So even a future NemoClaw
deployment would still want a `PreToolUse`-hook-style round-trip (§3.1b)
for that category; sandboxing mainly helps collapse the read-only
blast-radius noise, not the human-intent category.

**Genuinely open:** whether OpenShell's actual policy model is granular
enough to matter here (e.g. real "read-only, no exfiltration" enforced
modes) versus coarser than that — not yet verified. Flagged for a
follow-up research pass and, per Mike, a second opinion before acting on
it either way.

> **[R1 2026-07-16] Partially answered.** The NemoClaw policy schema is in
> fact granular (per-host/method/path network rules, landlock read-only vs.
> read-write paths, syscall-level containment) — see §9.2. So the "collapse
> read-only blast-radius noise into the sandbox" idea is technically viable.
> What it *cannot* do is the tool-semantic/human-intent asks (Gmail
> `get_thread`), which stay on §3.1's round-trip. The defense-in-depth
> principle is unchanged and now confirmed against source.

---

## 10. Security & operational model [R1 2026-07-16]

The pre-review doc had no section for three things that matter more than the
tool-permission mechanism it spent most of its length on: who may talk to the
server, what keeps the server alive, and what the logs leak. These were the
review's highest-severity gaps.

### 10.1 Server ingress — who is allowed to drive Marlow (DECIDED)

Every invocation runs as Mike with his full toolset and live credentials
(Gmail, Calendar, git push, the Pro-subscription token). The server is the
sole entry point (§2), so its inbound channel is the single most
security-critical surface in the system — and the original doc left the
device→server path as "some request channel." APNs is push-only
(server→device); message-send and permission-answer are inbound and were
undefined.

**Decided (Mike, 2026-07-16):**
- **No public ingress.** The server sits on a **Tailscale** (WireGuard
  overlay) tailnet; it has no internet-facing listener at all. iOS/macOS
  clients reach it only over the tailnet.
- **Device token, established via one-off pairing** — modelled on how
  NemoClaw's `device_pairing` approves a new client on first contact from an
  untrusted source (see `device_pairing`/`web_auth_method: bearer` in the
  manifests for prior art). First contact from a new device produces a
  pairing challenge Mike approves once; thereafter the device holds a token.
  This keeps a lost/rogue device from silently driving Marlow even inside the
  tailnet.

*Still open (needs Mike, low urgency):* whether the pairing-approval UI is a
push Marlow sends to an already-trusted device, or an out-of-band code — the
bootstrap-trust question ("how does the *first* device get approved"). Lands
during the ingress build (gate #3, §11).

### 10.2 Liveness / restart (DECIDED in principle)

The always-on server is now the sole path for cron *and* iOS; if it dies at
2am the morning briefing silently doesn't fire, iOS just times out, and any
"I'm down" alert path is the dead process itself.

**Decided:** run under **systemd with `Restart=always`** for process death.
For the harder *alive-but-wedged* case, add a **health-probe watchdog** —
this is exactly NemoClaw's own pattern: a `/health` endpoint plus a recovery
loop that relaunches the gateway when the probe fails (NemoClaw generates a
self-safe restart script so the watchdog can't kill itself; see
`src/lib/agent/gateway-restart-scripts.ts`). Marlow mirrors this: `/health`
endpoint + systemd for death + a lightweight watchdog for wedge.
Plus an **independent dead-man's switch** — a tiny external cron on a
*different* host that curls `/health` and texts Mike if Marlow has been down
> N hours (must be independent, since the in-box alert path dies with the
server).

### 10.3 Logging privacy (§3.4) — DECIDED direction

Full stdout capture (§3.4) persists Gmail bodies, calendar contents, and any
file Marlow reads, in plaintext, indefinitely — and if push payloads carry
content, Apple's APNs sees it in transit. Direction (Opus, adopted per Mike's
"go with your analysis"):
- Redaction policy + log rotation/retention (don't keep full tool output
  forever).
- **APNs is a wake-and-fetch signal only** — minimal/opaque payload; the iOS
  client pulls the actual content over the tailnet/TLS from the server. Push
  bodies never carry Gmail/calendar content through Apple's infrastructure.

### 10.4 Permission-decision correctness (from §3.1)

Rolled into the §3.2 work queue: each permission round-trip carries a
**request ID**; the phone UI shows *which* action it's approving; decisions
are **single-use** (a late answer to a timed-out/retried request is
discarded, not replayed). Fail-closed on timeout, per §3.1 [R1].

---

## 11. Build plan & Mike-in-the-loop gates [R1 2026-07-16]

Ordered so the load-bearing assumptions get tested by hand before code
depends on them. **Bold = Mike does this by hand.**

0. **Doc ratification.** Mike signs off on the [R1] changes; resolves the two
   remaining "still not decided" items when convenient (NemoClaw target
   yes/no §9.2; pairing-bootstrap UI §10.1). Neither blocks starting.
1. **Gate #1 — CLI contract spike (Mike + Marlow, by hand).** On the actual
   installed `claude` version, confirm: (a) headless auto-deny fall-through,
   (b) `--output-format json` `permission_denials` shape, (c) **the PreToolUse
   hook timeout/deny behavior** (§3.1 [R1] hinges on this), and **(d) the
   auth-expired failure signature** — exit code / stderr / JSON shape on an
   expired OAuth token, plus whether the stored credential exposes a readable
   expiry to pre-warn on (§3.7 [R2] hinges on this). This validates or breaks
   the whole permission *and* auth approach before any server code. Pin the CLI
   version once confirmed.
2. **Permission audit (independent, ships value now).** Run
   `fewer-permission-prompts`; implement the §3.6 layered model — working-tree
   read *allow* + credential/secret **deny** paths + argument-shape gating
   (`find … -exec`). Do this against the *current* interactive setup first; it
   de-risks headless and helps today's usage regardless of Marlow v2.
3. **Server skeleton + ingress + liveness (gate #3).** Work queue (§3.2),
   `/health` + systemd + watchdog + external dead-man's switch (§10.2),
   Tailscale ingress + device-token pairing (§10.1). **Mike:** approve the
   first device pairing by hand; confirm the dead-man's switch actually texts
   when the server is killed.
4. **Permission round-trip end-to-end (gate #4).** PreToolUse hook →
   server → push → bounded wait → chat-surfaced timeout message. **Mike:**
   trigger an `ask` from a real session, answer it on-device; then *ignore*
   one and confirm the graceful "skipped it" path.
5. **Cron cutover.** Move nightly review / weekly digest / morning briefing /
   fitness reminders / monthly check-in off tmux-inject onto the server entry
   point, under the strict unattended (never-prompt) policy (§3.1 [R1]).
6. **iOS/macOS app (SwiftUI multiplatform).** Chat UI, push handling,
   on-device whisper.cpp STT, permission-approval affordance. **Mike:**
   on-device STT latency check; end-to-end voice→Marlow→push round-trip.
7. **Parallel/independent track (Mike, manual, can start anytime):** Apple
   Developer account + APNs certificate setup — long-lead, gates #4/#6.
8. **Discord decision.** Keep as fallback channel or retire (§5.3) — decide
   once the iOS path is proven, not before.

**Decided this pass [R2 2026-07-16]:** build **standalone, OpenShell-shaped,
eyes-open to a future NemoClaw move** (§9.2 bottom line, §9.3). Not "target
NemoClaw as a platform now"; not foreclosed either. Auth-token lifecycle is a
first-class server concern (§3.7), folded into gate #1.

**Still not decided (need Mike, none block the start):**
- Pairing-bootstrap UI for the *first* trusted device (§10.1).
- Discord's fate (step 8).
