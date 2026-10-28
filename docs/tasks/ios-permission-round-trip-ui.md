# Task — iOS UI for the tool-permission round-trip

**Status: ✅ done, merged to `main` (2026-08-04).** Phase 1 and phase 2 both
confirmed on-device 2026-08-04. Found 2026-08-02 testing first light on a real
phone: Marlow asked to use a tool and was auto-denied with no prompt anywhere
(was START known gap #5). This closed the last missing piece of plan §4; the
server half had been built and tested since gate #4.

Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

**Superseded in part 2026-10-02 (`tool-approval-ux.md`):** the full-screen sheet
is now a centred card (`PermissionCard.swift`), and phase 2's local-notification
composition is gone. The push carries the server-written text itself, and the
lock screen offers **Review and Deny only, never Allow** (Mike's call: the user
always opens the app to see the real request). The `FULL` category and
`notificationSummary` below no longer exist. Everything else here is history.

Spun out rather than folded in: multi-device "approval spam" routing
(`multi-device-permission-routing.md`, placeholder); a CI trigger fix
(`pull_request`'s path filter matched the PR's whole diff on every synchronize, so
a docs-only merge into the PR re-ran the macOS job; switched to `push` plus
`workflow_dispatch`). Cron's never-prompt policy (unattended permission requests
fail closed, no push) shipped separately (`v2-dogfood-parity.md`, stage 4 runbook
step D); unprompted turns in the transcript are `ios-unsolicited-turns.md`.
`invoke.py` surfaces `permission_denials` / `had_permission_denial`, but no view
renders it; nobody has built a "Marlow was denied a tool here" affordance.

## What shipped

The server already had the round-trip (hook, registry, long-poll, push, answer
endpoint); the client wiring was the gap.

- **§3 — `GET /v1/permissions`, side-effect-free.** Deliberately not
  `/v1/pending`, whose `ready_undelivered_turns()` marks turns delivered as it
  serves them, which a 2s poll would have silently drained. Tested not to consume.
- **§4 — Phase 1, in-app approval.** A permission request can only exist while a
  turn is RUNNING, and `ChatStore.pollLoop` already polls every 2s then, so the
  sheet piggybacks on that poll rather than a new timer: `GET /v1/permissions`
  feeds `ChatStore.pendingPermission`, and `ChatView` shows a modal sheet with a
  live countdown and Allow/Deny. (The shared `.sheet(item:)` binding is
  load-bearing: SwiftUI presents one sheet at a time per view, and a separate
  Settings sheet silently blocked an incoming request until dismissed, up to the
  ~55 s auto-deny. See `voice-mode.md` on the one unexplained settings freeze.)
- **§5 — Phase 2, locked/backgrounded phone.**
  `AppDelegate.didReceiveRemoteNotification` fetched `GET /v1/permissions` and
  composed a **local** notification (the no-content-through-Apple pattern
  `server/push.py` already used) with the tool name and a truncated `toolInput`
  summary, so Allow was never a blind approval. Two categories:
  `PERMISSION_REQUEST_FULL` (Allow/Deny) and `PERMISSION_REQUEST_TRUNCATED`
  (Review/Deny), Review being `.foreground` and landing on phase 1's sheet.
  `answerPermission` fires straight from the notification response handler. Server
  side: `apns-expiration` capped at `t_hook_seconds` (55s) and
  `interruption-level: time-sensitive` on `PERMISSION_REQUEST` pushes only (the
  Focus window was the worry), with the time-sensitive entitlement.
- **Phase 3 (double-banner cleanup) was never needed.**

No Swift test coverage of `AppDelegate`/`APNsRegistrar` (they need a live
`UIKit`/`UNUserNotificationCenter`, not a fake `ChatAPI`), so the manual §7
on-device acceptance test carried that risk.

## On-device results

**2026-08-03:** phase 1's in-app sheet round-tripped twice for real (Allow and
Deny both worked; the tool ran / was correctly blocked). It also surfaced three
real bugs beyond the spec:

1. `marlow`'s `~/.claude/settings.json` had no `hooks.PreToolUse` wired at all:
   **the round-trip had never fired in production before this test.** A temporary
   all-tools-prompt config went live on the box (START known gap #7 for the
   real-policy follow-up).
2. The generic remote push banner and our own composed local notification both
   showed while the app was foregrounded, though the poll loop already surfaces
   the same thing live. Fixed by suppressing presentation whenever
   `UIApplication.shared.applicationState == .active`.
3. **The 2026-08-04 lesson (cited by `hooks/pretooluse.py`):** the hook's
   `permissionDecisionReason` was a static engineering string ("phone round-trip
   via /internal/permission-request") regardless of outcome, and Marlow read a
   denial's reason as looking like injected/spoofed tool output and flagged it
   instead of just registering the denial. Now distinct, natural-language text per
   outcome. Anything the hook says to the model should read as a person would say
   it, never as jargon.

**§7 acceptance, 2026-08-04:** the first locked-phone notification test was
inconclusive (Sleep Focus was on, which very plausibly swallowed the push before
`time-sensitive` could matter). **Retested clean with Focus off: pass.** Two
locked-phone permission pushes arrived, each showing only the generic/truncated
summary until Face ID ran (Show Previews gating working as designed) and neither
showing inline Allow/Deny, also correct, since both were long `Bash` commands that
landed in the `TRUNCATED` category. Tapping opened the app straight to phase 1's
sheet with the countdown bar. Only the `FULL` category's inline Allow button was
not separately exercised (same code path, shorter command, lower risk).

## Deferred: a friendlier summary, without trusting the narrator

Raised 2026-08-03. `{"command": "find /Users/mike -name '*.tmp' -mtime +30
-delete"}` is not friendly to parse on a 55-second clock, and the obvious fix is
to have **Marlow describe its own intent**, with an expander for the raw request.
Deferred, reasoning recorded so it isn't re-litigated:

**A model-authored summary is worthless in exactly the case the gate exists
for.** The prompt is there for when Marlow has been talked into something (a
poisoned web page, a malicious file, a crafted email). A model that can be induced
to run the command can be induced to narrate it as "just tidying up some temp
files." The narration and the tool call are the *same trust level*, so a summary
derived from the model can't be the basis of a decision about the model. Worse, a
friendly sentence next to a raw command reliably gets read *instead of* the
command.

**The safe version comes from our code.** A deterministic renderer computes the
summary from the actual bytes that will execute: `Write` → "write 2.3 KB to
~/marlow/memory/foo.md"; `Bash` → the command verbatim plus derived annotations
("deletes files", "touches 3 paths outside the working dir", "network egress to
api.github.com"). Trustworthy because grounded in the request rather than a claim
about it, and it lands as an extension of `server/boundaries.py`, which already
classifies tool inputs to *deny* rather than to *describe*. Phase 1's raw
`displayString` is the honest, zero-risk floor.

**The narration isn't even available today.** The PreToolUse payload carries
`tool_name`, `tool_input`, `session_id` and nothing else, no reasoning or
preceding text; capturing intent would mean parsing stream-json, real plumbing that
doesn't exist.

**Decision rule:** raw/derived rendering first. Revisit only if Mike finds himself
unable to judge real prompts quickly, and then extend `boundaries.py` into a
classifier. Do **not** put model-authored prose in the approval path. If it ever
appears, it goes *below* the deterministic facts, marked as Marlow's claim, never
the only thing on screen.

## Future — "this is the edit Marlow wants to make", as a red/green diff

Mike, 2026-08-07. The same argument carried to its end: a one-line `displayString`
says *which* file is written, and for an `Edit` that isn't enough to approve on.
`Edit`'s `tool_input` already holds `old_string` and `new_string`, so a red/green
diff is a **rendering** of data in the payload, not new plumbing, the cheap
version of legibility unlike the narration idea. It is consistent with the
decision rule: deterministic, grounded in the bytes that will execute.

**It matters more than it looks.** Mike's 2026-08-07 call on Marlow-authored
scripts (`marlow-working-dir-git-policy.md`) leaves script edits gated by a phone
prompt and nothing else, no promotion step, deliberately. So the prompt *is* the
control for a script that will later run, and approving "Edit trello-sync.sh" blind
is thin. The diff turns that from accepted risk into a real review step; sequence
it accordingly once Marlow starts writing its own tools again.

Scope: `Write` (whole new file) and `Edit` (in-place change) want different
displays, full content with syntax colouring versus a two-sided diff. Start with
`Edit`, the common case and the one where a summary is weakest.
