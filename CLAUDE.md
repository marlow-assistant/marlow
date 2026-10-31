# CLAUDE.md — for sessions building marlow-server

You are in **builder mode** for Marlow v2, working on the *software*. You are
**not** acting as Marlow the assistant. (The assistant's own identity lives in
`identity/` as a template and is seeded onto a deployed box, not here — don't
confuse `identity/CLAUDE.md`, which is that template, with this file.)

**Start every session by reading `docs/marlow-v2-START.md`** — it has the
current gate status, the read-first order, and where things stand. Then build
from `docs/marlow-v2-implementation-plan.md`.

## Hard constraints
- **CLI, never the Agent SDK.** `claude -p` / `--continue` / `--resume` draws on
  the Pro subscription; the SDK forces API-key billing. This is the whole point.
- **`server/invoke.py` is the only module that shells out to `claude -p`,** in a
  **scrubbed env** (strip `CLAUDE_CODE_*`, `CLAUDECODE`, `AI_AGENT`; stdin
  `</dev/null`). Children inherit the toolset otherwise.
  - **No `--strict-mcp-config`.** It also strips the account's Claude.ai
    connectors, where Gmail and Calendar live, so the morning jobs can't run
    with it; there is no middle path (`docs/tasks/v2-dogfood-parity.md`). The
    guard instead is **`boundaries.AGENT_CONFIG_PATHS`**: without the flag the
    CLI honours every MCP config source, and an `mcpServers` entry is a command
    that runs at CLI *startup* — before the first tool call, so before the hook
    exists to object. Those files being unwritable is the constraint. Don't
    re-add the flag, and don't weaken the config denies.
- **Detect tool denials via a non-empty `permission_denials` array — not exit
  code.** A denied tool still reports `exit=0` / `is_error=false`.
- **Permission round-trip = fall-through to the PreToolUse hook, not
  `permissions.ask`** (ask auto-denies in headless and overrides a hook allow).
- **One work queue serializes all `claude -p` calls.** No concurrent invocations.
- **Public ingress only via per-box Funnel,** after the hardening in
  `docs/tasks/funnel-ingress.md` (invite pairing, revocation, body caps,
  `/health` behind the token, the surface-pinning test). Off by default, and
  never on Mike's box; otherwise Tailscale only, device-token pairing.
- **"OpenShell-shaped"** boundaries (fs path-sets, egress allowlist, env secrets)
  in `server/boundaries.py`; standalone-correct first.

## Working rules
- **Tests are load-bearing here** — this is shrink-wrap software Mike wants to
  trust. Keep the suite green; add tests with behavior. Run `.venv/bin/pytest -q -n 4`
  before committing.
- **Test first** (Mike, 2026-10-25): write the test, watch it fail for the
  right reason, then implement. That is the proof the test checks the fix.
  Don't build the fix first and revert it to show the test can fail.
- **Parallel features each get a branch and a PR** (Mike, 2026-10-31). The
  repo is public, and other sessions or contributors may be writing at the
  same time. Task docs are reasoning-first, not handoff specs.
- **A user-facing feature is done only when both clients have it** (Mike,
  2026-10-05): iOS *and* `web/`, which is the Android client. The Usage panel
  and the approval card shipped iOS-only and an Android user couldn't find them. Scope
  both halves up front; never write "shipped" in a Status line for one client
  ("iOS shipped, web not started"), and say so when Mike calls one-sided work
  finished.
- **Changed a harness affordance? Update the identity in the same PR.** A new
  store the agent writes, a lifted restriction, a path it must know — in
  `identity/CLAUDE.md`, which `deploy/update.sh` installs on every box that has
  `PREFERENCES.md` (`docs/tasks/box-upgrades.md`). A box without it still needs
  its live `~/marlow-identity/CLAUDE.md` edited by hand. A feature the identity
  doesn't mention gets routed around: told nothing about a new schedules store,
  Marlow reached for a scheduling skill and `sleep 300`, with every test
  passing. See `docs/marlow-v2-workflow.md` step 7.
- **A change the user would notice gets a `changes.json` entry in the same
  PR** — always for anything changing what Marlow can reach, does
  unattended, stores or sends off-box, or how it behaves. Policy, categories
  and `part` (server vs app): `docs/tasks/whats-new.md`. The card on both
  clients reads that file; a change without an entry lands silently.
- **Don't drop a comment that records *why*.** Several encode device-only
  findings with dates — if the code moves in a refactor, the comment moves with
  it. Deleting one costs the next session the bug it prevents.
- **iOS: run `xcodegen generate` after adding or deleting any source file.**
  `Marlow.xcodeproj` is generated from `ios/Marlow/project.yml`. A new test file
  is otherwise *not compiled* — it fails nothing and the suite reports green
  without it. Check the test count moved.
- **CI runs on PRs and on `main`, not on a bare branch push** (`ci`, the
  pytest job named `test`; `ios` only when a push touches `ios/`). So the
  first gate is `.githooks/pre-push`, which blocks a red push locally (enable
  per clone: `git config core.hooksPath .githooks`). Say "green in CI" only
  when the PR's checks have reported; otherwise say the local suite passed and
  give the count.
- **Review is done by tools, not Mike's line-by-line reading.** Run
  `/code-review high` yourself on non-trivial changes, plus `/security-review`
  when the change touches the permission round-trip, egress, credentials, or
  auth. Surface findings as a short list, not a diff to rubber-stamp; if a
  review command refuses to launch, say so plainly. `/code-review ultra` is
  Mike-triggered (billed) — don't try to launch it. Full flow:
  `docs/marlow-v2-workflow.md`.
- **Commit when it makes sense, and push feature branches freely. Ask Mike
  before opening a PR or landing anything on `main`** — a merge, or a direct
  push. `main` is the release.
- **Work in a worktree by default.** Mike runs several sessions on this repo
  at once, and two of them writing in the shared checkout have collided. If
  this session is in the repo root (not under `.claude/worktrees/`), call
  `EnterWorktree` before the first edit. It branches from a fresh
  `origin/main`. Stay in the root only for read-only work, or for a docs-only
  change Mike has said goes straight to `main`. Even then, run `git pull
  --ff-only` first, and before pushing run `git log origin/main..HEAD --stat`
  to check every commit is yours.
- **Sub-agents are allowed where they genuinely help:** work that is parallel,
  read-only and self-contained — an adversarial second pass over
  security-sensitive code, a broad search whose file dumps you don't need,
  several independent investigations at once. The bar is that a cold-start
  agent can do it without re-deriving this session's context, so don't spawn
  one for something you already have loaded, and don't fan out where one
  careful pass would do.
- **Never nest `claude -p` inside your own session** to test CLI behavior — it
  inherits your harness env and gives false results. Hand Mike a copy-paste
  script for a clean shell instead.
- **Name no person but Mike and Taylor in anything that goes into this
  repo** (Mike, 2026-10-21; Taylor allowed 2026-10-25, a matter of public
  record): files, comments, tests, commit messages, PR text. Other users
  are "a second user on the box", "the Android user", "a night-owl user" —
  describe the role, not the person. Never a username or handle.
- **Never commit real content from a box or an account** (Mike,
  2026-10-25), not even as evidence: no quoted email, calendar entries,
  messages, briefs, memory, health, money, places or trips. Describe the
  shape ("an appointment next week", "a bank alert"). Grep before
  committing: once pushed, removing it takes a new repo.
- **A precise GPS fix is PII** (Mike, 2026-10-26): it can be an address.
  Keep every coordinate in the repo to 2 decimals (~1 km) or coarser, as the
  server sends them. More digits read as a real address, even when made up
  (Mike, 2026-10-27: one placeholder sat on a place we'd visited). A test of
  rounding or of a sub-km distance threshold may use more, with invented
  repeating digits (`46.814949, -71.213838`), never a fix copied from a
  device, log or map, and never a place anyone has been. Token greps don't
  catch coordinates, so look.
- **Docs and comments don't cite commits or PRs** (Mike, 2026-10-26).
  Describe the change and date it ("the scroll-follow fix, 2026-09-08");
  git keeps the hashes. A passage that leans on one is written relative to
  a moment and goes stale with it, and an instruction like "before the
  first update past X" is dead once every box is past it — delete it.
- **Never name other AI agents or assistants in repo files** (Mike,
  2026-10-21). Docs, comments, commit messages: say "other AI assistants" or
  describe the behaviour instead. Marlow's own name and the tools this repo
  builds on (Claude, the CLI) are fine. Two exceptions (Mike, 2026-10-27):
  the artwork provenance line in `NOTICE`, and plain technical facts about
  the agents NemoClaw/OpenShell run (OpenClaw, Hermes) in the design docs.
  Never in a comparison with Marlow or a judgment of them.
- Mike is a senior technologist — be direct, lead with the answer, one concrete
  recommendation over a survey. Don't start large code before he picks a direction.
