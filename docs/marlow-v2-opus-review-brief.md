# Marlow v2 — Opus Design Review Brief

**Purpose:** Adversarial architecture review of `docs/marlow-v2-design.md`
before implementation starts (Sonnet will be the builder). You are the
second pair of eyes — find design/architecture flaws the authors,
who wrote this doc across several sessions, are too close to see.

**How to use this brief:** Paste this file's contents into a fresh Opus
session along with the full contents of `docs/marlow-v2-design.md`. For
best results also share `CLAUDE.md` and `memory/projects.md` from the
same repo so you can judge whether design choices actually fit the
surrounding system (existing skills, conventions, behavior rules), not
just whether the doc is internally consistent.

---

## What NOT to do

Don't give a general "does this look good" pass — that produces
compliments, not findings. Don't treat any section as pre-approved just
because it's written in a confident, decided tone; some sections are
explicitly marked tentative (see below) and should be attacked as hard
as the rest.

## Priority targets — attack these specifically

### 1. §3.1 — Permission model (PreToolUse hook round-trip)
The plan is a `PreToolUse` hook that blocks synchronously, calls out to
a server component, which pushes a question to an iOS app and waits for
a human response, then returns allow/deny back through the hook.

Stress-test:
- What happens if the phone is unreachable, asleep, or the human doesn't
  respond — does Claude Code's hook-blocking have a sane timeout
  behavior, or does this hang a session indefinitely? Is "fail closed"
  (deny) actually the right default, or does that break workflows that
  need the answer?
- Concurrency: can two tool calls needing human decisions collide (two
  pending questions to one phone at once)? Is there a defined ordering /
  queueing model?
- Is there a replay/idempotency risk if the hook retries after a
  network blip and the human already answered once?
- Does synchronous blocking in a hook script actually behave safely
  across `--resume`/`--continue` session boundaries, or could a
  crash/restart of the server component orphan a session mid-wait?

### 2. §3.2 — Session/concurrency model
Decision: separate sessions per trigger source (iOS chat vs. cron),
shared memory files as the cross-session bridge.

Stress-test:
- Is memory/*.md actually sufficient as a synchronization mechanism, or
  is there a race condition where a cron-triggered write and an
  iOS-triggered write land concurrently and one silently overwrites the
  other (no locking described anywhere in the doc)?
- Does "separate sessions" create a UX problem where the phone user
  doesn't see cron-originated actions/context until the next memory
  read, i.e. staleness windows?

### 3. §5 / §7 — Server component built on `claude -p` CLI, not the Agent SDK
Decision was driven primarily by billing (Pro subscription vs. API key).
Stress-test:
- Is there any single point of failure in relying on an undocumented or
  loosely-documented CLI behavior (headless mode auto-deny behavior,
  `--output-format json`'s `permission_denials` array) that could change
  under Claude Code CLI updates without warning, silently breaking the
  server component?
- Given the whole architecture depends on `claude -p` continuing to
  support this interaction pattern indefinitely, is there a fallback
  plan if that CLI surface changes or regresses?

### 4. §9 / §9.1 — NemoClaw platform question (explicitly tentative)
This section documents a single research pass (not verified firsthand,
some content fetched via AI summarization rather than raw source) on an
alpha, actively-mid-migration open source project. Treat this section
with the LEAST prior confidence of anything in the doc.

Stress-test:
- Is the "manifest.yaml = language-agnostic contract" read actually
  correct, or is there a reason (missing from the research) that a
  Node-based control plane would still meaningfully favor a
  TypeScript-native agent in practice (tooling, debugging, ecosystem
  libraries for the MCP bridge adapter, etc.)?
- Is the "defense-in-depth, not dependency" principle in §9.1 actually
  achievable in practice, or does building genuinely-portable
  Unix-permission-based safety (§3.6) plus optional-later
  OpenShell-sandboxed safety (§9.1) risk becoming two permission systems
  that drift out of sync / get maintained inconsistently?
- Does targeting NemoClaw at all introduce material complexity or
  constraints elsewhere in the architecture (e.g. incompatible with the
  SwiftUI-client-talks-to-standalone-Python-server model in §8) that
  isn't visible without reading NemoClaw's actual sandboxing/networking
  model more deeply than this doc's research pass did?

### 5. §3.6 — Wider default permission allow-list
Stress-test:
- Is the read-only/side-effect-free command list (`cat`, `ls`, `grep`,
  `find`, `head`, `tail`, `stat`) actually safe to blanket-allow, or are
  there commands in that shape that have side-effect or
  information-disclosure risk not captured by "read-only" framing (e.g.
  `find -exec`, `grep` reading files outside intended scope, `stat` on
  sensitive paths)?

## General architecture questions (lower priority, but worth a pass)

- Single point of failure: the "always-on server component" is now the
  sole entry point for cron, iOS, and any future trigger (§2). What's
  the failure/restart/monitoring story if that process dies? Nothing in
  the doc currently addresses this.
- Is SwiftUI multiplatform (§8.1) actually going to deliver a
  good-enough macOS experience, or is "iOS + macOS from one codebase"
  optimistic given known SwiftUI multiplatform rough edges (this is a
  request for a reality check, not a rhetorical question)?
- Logging (§3.4) — is anything in the log design a privacy/security
  concern given it's meant to capture full stdout/response content,
  potentially including sensitive tool outputs (Gmail content, etc.)?

## Output format requested

For each finding: cite the section, state the concrete failure scenario
(not just "this could be a problem" — what input/state causes what
observable failure), and rate severity (blocks implementation / should
fix before implementation / worth tracking but not blocking). Do not
soften findings to be diplomatic — this review exists specifically to
surface what a friendlier pass would miss.
