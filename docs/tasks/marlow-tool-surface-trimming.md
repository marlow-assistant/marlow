# Task — trim Marlow's tool surface with `--disallowedTools`

**Status: SHIPPED.** Merged and **deployed to the box 2026-08-24**, verified live
by listing (§8). `/code-review medium` produced four findings, all resolved (§4,
§7); `/security-review` returned no findings — the change is deny-only and
strictly narrowing, and every tool named was already fail-closed. Local suite
green, 809 passed. **Context saving measured live: 4,901 tokens per turn (~18%).**

Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

Opened because Marlow reaches for tools that are not its to reach for. Most
recent: it proposed `CronCreate` for a "remind me later" request, Mike approved
it, and then asked what `CronCreate` actually *is*. Before that, `RemoteTrigger`
for the same want. Both are Claude Code harness tools with nothing to do with this
server's scheduling.

## 1. Why the surface is bigger than the product

The connectors are attached to **Mike's Claude account**, and Marlow runs
`claude -p` as that account (gate #1's premise: the CLI draws on the Pro
subscription). Anything Mike connects for his own use — Trello, Excalidraw,
Drive, Mermaid Chart — lands in Marlow's tool listing too, with no per-consumer
split short of a second subscription.

`--strict-mcp-config` would scope this and **cannot be used**: it also strips the
Claude.ai connectors where Gmail and Calendar live (dropped 2026-08-06;
`docs/tasks/v2-dogfood-parity.md`). `--disallowedTools` is a different flag and
doesn't carry that cost.

## 2. What the probes established

Run as `marlow` on the box, CLI 2.1.221 (same as the dev Mac), `-p` with a "list
every tool name" prompt.

- **Probe A — exact name.** `--disallowedTools "mcp__claude_ai_Trello__trelloReadBoard"`
  → that one name absent; the other fifteen Trello tools still there.
- **Probe B — bare server prefix.** `--disallowedTools "mcp__claude_ai_Trello"` →
  **all sixteen** absent. No wildcard needed; the server name alone.

The load-bearing finding: **`--disallowedTools` elides the name from context, it
doesn't merely gate the call.** That makes it a context lever rather than a
second permission layer, and it wasn't obvious in advance (the flag is documented
as a deny). And server-prefix denial means four short entries rather than ~40
tool names.

**Baseline:** probe A's envelope reported 16,652 cache-read + 10,735
cache-creation ≈ **27k tokens of context to answer one throwaway question.**

## 3. Corrects an error in the sibling doc

`marlow-settings-permissions-policy.md`'s tools section originally concluded,
from Marlow reporting its deferred list arriving in three batches, that the tool
surface "materialises as the MCP servers finish connecting" so no static list
could cover it. Probe A disproves it: one turn, no `ToolSearch` call, full
connector set listed up front. The batching was MCP servers still connecting
during startup — a race, not a property. That paragraph has been corrected; the
wrong version argued against exactly the mechanism this doc rests on.

## 4. The list — decided 2026-08-24

**(a) Connectors that are Mike's, not Marlow's — DENIED.** `mcp__claude_ai_Trello`,
`..._Excalidraw`, `..._Google_Drive`, `..._Mermaid_Chart`. Low risk: none is in
`VETTED_PATHLESS_TOOLS`, so none was reachable without a phone prompt anyway.
Denying them removes a prompt Mike would always refuse and the context that
invites it.

**(b) Harness tools — DENIED.** Twelve: `CronCreate`, `CronDelete`, `CronList`,
`RemoteTrigger`, `DesignSync`, `EnterWorktree`, `ExitWorktree`, `TaskOutput`,
`TaskStop`, `NotebookEdit`, **`Agent`**, **`ListAgents`**. Two are proven friction
(`CronCreate`, `RemoteTrigger`); the rest are coding-harness scaffolding.

**`Agent`/`ListAgents` were added after review and are the interesting part.**
The first draft denied `TaskOutput`/`TaskStop` while leaving `Agent` reachable —
a dead end `/code-review medium` caught (Mike approves an `Agent` call, the
subagent runs, Marlow has no tool to collect the result). The answer came out
**no subagents**, for reasons about *this server*:

- **The queue serializes every `claude -p` call and `invoke.py`'s timeout is 20
  minutes.** One runaway subagent blocks Mike's next message and the morning cron
  alike. Nothing else here can block that long.
- **The activity stream can't show it.** The parser returns on the first
  `tool_use` block, so the client shows "using Agent" and then nothing; nothing in
  `server/` or `hooks/` handles `parent_tool_use_id` (grepped, zero hits).
- **The permission round-trip degrades.** Each nested call needing approval fires
  its own 55s fail-closed prompt for work Mike didn't scope; in cron they are all
  auto-denied, so the subagent flails and returns nothing.
- **It is not wanted.** `Agent` fans out searches across a large codebase; Marlow
  reads mail, summarises, reminds and answers.

Bonus: they are the only **eagerly loaded** tools denied here, so they carry full
JSON schemas on every turn including cron — the largest per-turn saving.
**Reopening it means fixing the queue, the activity stream and the permission
window first**, not deleting two lines.

**Removed before merge: the three MCP-resource readers** (`ListMcpResourcesTool`,
`ReadMcpResourceTool`, `ReadMcpResourceDirTool`). Never observed causing friction,
and nobody had checked whether a connector exposes *resources* Marlow reaches
through them. Speculative hardening on a silent failure mode; add them back if
something reaches for one. **`Monitor`, `SendMessage` and `PushNotification`**
stay in, undecided: plausible affordances rather than proven noise.

**(c) The Gmail/Calendar write tools — NOT DENIED. Mike's call, 2026-08-24.**
Denying them is the largest context win by far (Gmail lists ~28 tools, exactly
two granted) but a **behavioural change, not a cleanup**: those calls fall through
`hooks/pretooluse.py` to a phone prompt, which is how Mike keeps the per-occasion
decision (`VETTED_PATHLESS_TOOLS`'s note on `get_thread`/`get_message`, 2026-08-07).
A CLI-level deny removes the option to say yes, permanently and silently — Marlow
would report it cannot send mail rather than asking. Agreed the same evening;
`test_gmail_and_calendar_write_tools_are_not_trimmed` pins it, so reversing it
means editing a test.

**The number: 27,387 → 22,486, a saving of 4,901 tokens per turn (~18%)**, measured
2026-08-24 on the box with the same prompt as the §2 baseline. (a)+(b) bought the
bulk; (c) would spend a real option (approving a send from the phone) for the
remainder. **Recommend leaving (c) permanently undone** unless something changes;
revisit only with a fresh measurement.

## 5. Where it lands

**`permissions.deny` in `deploy/marlow-settings.json`.** Probe C (over `ssh` as
`marlow`, 2026-08-24): a server-scope entry in the deny list elides the name
exactly as the argv flag does, `permission_denials` empty. So `server/invoke.py`
is **not touched** — the argv route would have put this on the CLI-contract path
and pulled in a `/code-review high`. Two probe traps: **`claude -p` must have
`</dev/null`** (the first attempt hung silently, reading the piped shell script as
stdin), and `claude` is not on a non-login ssh PATH (use
`/home/marlow/.local/bin/claude` or `bash -lc`).

## 6. Verify by listing, never by reading the file

**A deny rule can be silently inert.** On 2026-08-23 five `Write(...)` denies
turned out not to participate in file-permission checks at all
(`marlow-settings-permissions-policy.md`); an absent error looks exactly like
success. So the acceptance check is the probe: run the listing and confirm the
denied names are **gone from the output**. That needs the box, as `marlow`.

And none of this is urgent: every tool in §4(a)/(b) was already fail-closed
(grepped 2026-08-24; none in `VETTED_PATHLESS_TOOLS` or `marlow-settings.json`,
so each fell through to `request_decision`). **This task removes friction and
context, not an exposure.** Don't let a review re-file it as a security fix.

## 7. Tests (lessons)

Three, in `tests/test_marlow_settings.py`. The important one,
`test_the_trim_never_denies_something_that_is_granted`: a deny wins over an allow
and also **elides the name**, so denying something Marlow uses would show up not
in `permission_denials` but as Marlow calmly reporting it cannot read mail, with
nothing saying why — the same silent shape as the inert denies, running the other
way. It checks the allow list *and* `VETTED_PATHLESS_TOOLS`, and the **prefix**
relation, because a stray `mcp__claude_ai_Gmail` server-scope entry would kill
`search_threads` while passing any exact-name comparison.

## 8. Verified before merge, 2026-08-24

The candidate policy was `scp`'d to the box and run through the §6 listing:

- **All 16 denied entries elided.** Exact-name matching, not substring — the
  first pass used `in` and produced a false positive on "Trello" in the model's
  own prose.
- **Nothing granted was lost:** `WebSearch`, `ToolSearch`, `Bash`, `Skill`, and
  Gmail/Calendar both halves (`search_threads`, `send_message`, `create_event`
  still listed — §4(c) holding).
- **Context 27,387 → 22,486 tokens.**

**Not ours:** `TodoWrite` did not appear in the listing, nor in the §2 baseline,
though it is in `permissions.allow` and `VETTED_PATHLESS_TOOLS` — a dead entry on
CLI 2.1.221 (same family as the inert `Write(...)` denies; noted in the
settings-policy doc's cleanup section, not fixed here).

The work was not handed to an implementor (2026-08-24): four strings and thirteen
tool names, and what made it safe — the three probes and the §4(c) judgement — was
already resolved in the speccing session.

## 9. Deploying it — the two-step

Done 2026-08-24. `update.sh` ships **code only**; the live policy is a copy at
`/home/marlow/.claude/settings.json`, so the change is inert on the box until the
`cp` runs. The `cp` was genuinely required: right after `update.sh` the live
policy still held the old 20 entries (the same trap that would have silently
reverted `effortLevel` in 2026-08-15). No service restart needed — each
`claude -p` reads settings at startup, so it took effect on the next turn with one
prompt-cache re-warm. Post-deploy listing: nothing denied still listed, nothing
granted lost.

A trap for reading timestamps on the box: it runs **MDT** while commits here are
stamped **ADT**, three hours apart. Compare git SHAs, not mtimes.

## Reference

`server/invoke.py` (the `--strict-mcp-config` removal comment),
`server/boundaries.py` `VETTED_PATHLESS_TOOLS`, `deploy/marlow-settings.json`,
`hooks/pretooluse.py`, `docs/tasks/marlow-settings-permissions-policy.md`,
`docs/tasks/marlow-harness-affordances.md` (§4(b) overlaps it),
`docs/tasks/v2-dogfood-parity.md`.
