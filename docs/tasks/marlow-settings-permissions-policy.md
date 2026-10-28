# Task — a real permissions policy for the `marlow` user

**Status:** shipped. Built 2026-08-04, installed on the box 2026-08-06; closes
START's Known gap #7. The hook vets `Read`/`Glob`/`Grep`/`Edit`/`Write` locally
(not just Bash) from 2026-08-06; pathless tools from 2026-08-07; the
`<working_dir>/.git` deny from 2026-08-08. Since 2026-09-06 `update.sh` syncs
the live copy at `~/.claude/settings.json` itself and prints `policy CHANGED`
when it moves.
Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

**Shape:** `deploy/marlow-settings.json` (one file, every turn); the unattended
never-prompt policy in `server/active_turn.py` + `server/app.py`; Bash vetting
in `server/bash_policy.py`; path vetting in `server/file_policy.py`; the
pathless set in `server/boundaries.py`; drift tests in
`tests/test_marlow_settings.py`.

## What this replaces, and what it is for

From 2026-08-03 the box ran a *temporary* settings.json: the `PreToolUse` hook
wired and **no `permissions.allow` at all**, so every tool call fell through to
a phone prompt. That proved the round-trip, and it is the most restrictive
state the system can be in, but it made the assistant unusable day to day and
unattended operation impossible: a cron job at 3am stalls 55 s per tool call
and then fails closed.

**This is not a security gate, it is the unattended-operation gate.** Writing an
allow list deliberately *loosens* the box, in exchange for it being useful when
Mike isn't holding the phone. Framing it as "hardening" gets the trade-off
backwards.

## The probes that shaped it

- **`scripts/probe-allow-vs-hook.sh`, 2026-08-04 (Mike's MacBook, and the box as
  the admin user, CLI 2.1.221, both controls green): a tool matched by `permissions.allow`
  still fires the `PreToolUse` hook.** So `boundaries.py`'s secret-path and
  argument-shape denies stay a live backstop underneath every allow entry.
- **Same script, runs 4-5, 2026-08-05, on the box: a hook `deny` binds for an
  allow-matched tool** (the allow-matched `touch` was not created). Full
  contract: the hook fires for allow-matched tools *and* its decision binds.
  That is what let the path-token allowlist live in the hook, and lets the
  allow list be generous on convenience grounds without that being a security
  decision.
- **The CLI's own filesystem sandbox is a layer nobody had accounted for.** The
  no-hook control failed with "Blocked by the sandbox — writes are limited to
  /Users/mike/nemoclaw/marlow-server". It sits below allow/deny and the hook, and
  confines writes to the working directory. A hook `allow` overrides it; a bare
  `permissions.allow` does not. It looked like default behaviour at 2.1.221.
- **A fourth layer:** a first canary in `~/.ssh` made the no-bwrap control fail,
  because the CLI appears to refuse that path on its own account, above allow
  rules and independent of the hook. Not ours and not guaranteed across
  versions, and it made `~/.ssh` useless as a measuring stick. Canary moved to
  `~/.marlow`, which the CLI has no opinion about.

**CLI version: the pin is a gate, not a freeze.** `DISABLE_AUTOUPDATER=1` buys
*deliberate* change, so a bump arrives through `update.sh` as an event (the CLI
had drifted 2.1.211 → 2.1.216 unbidden before the pin). Bump attended, and
re-run the probe on every deliberate bump: this policy rests on behaviour only
ever confirmed at one version, and new confinement layers arrive with versions.
Do the bump attended: the write sandbox above was present at .221 on both
machines and the `marlow` CLI at .216 predates it, and meeting a new
confinement layer attended is much cheaper than meeting it during a 3am
consolidation run.

**Still open: the CLI write-sandbox check on the box was never closed.** If the
sandbox is active there, it silently constrains every write Marlow makes outside
`MARLOW_WORKING_DIR` regardless of what this policy says. That mostly agrees with
what we want, but a surprise layer that "mostly agrees" is the kind that misleads
later. Check `claude --version` on the box, and whether a write outside the
working dir is refused.

## Decision: allowlist over denylist (Mike, 2026-08-04)

Found by `/security-review`, 2026-08-04: `Bash(git diff:*)` read any file on the
box. `git diff --no-index /home/marlow/.ssh/id_ed25519 /dev/null` prints the
file, and `git diff --output=<file>` is the write direction. `git` was a
file-reading verb the "no file-reading verb in the Bash allow list" rule never
contemplated, and nothing else caught it: the `deny` block denied `Read(...)` (a
different tool) and `Bash(cat ...)`, and the hook **did not inspect Bash command
strings for secret paths**, only `file_path`/`path`/`notebook_path` keys. With
`WebFetch` allowed unattended and `egress.nft` deferred, that was a complete
exfiltration path with no human in the loop. Reachable: `.credentials.json`, the
deploy key, `device_tokens.json` (gates the phone approval channel), the APNs
`.p8`s, every `.env`.

**Mike: allow-list, don't deny-list.** The denylist shape is what produced this;
the drift test passed `git diff` because `git` was not a name anyone thought to
list. But the allowlist cannot live in settings.json: Bash rules are
command-prefix matches with no path awareness, so any refinement there is
another denylist. It goes in the hook, and the two files split by role:

- **settings.json `allow`**: a prompt-reduction dial. Convenience, not a boundary.
- **the hook**: the boundary, applied to every Bash call. Vet by *path token*:
  every path-shaped argument must resolve inside the working tree. That kills
  `--no-index` without knowing anything about git, and the next verb nobody
  thought of. `git` gets a subcommand allowlist and its options stay unfettered,
  because the harm was never the option, it was the path the option reached.

`SECRET_PATHS` stays a denylist on purpose: the small enumerated set that must
be refused *even when Mike thumbs allow*.

### What shipped

`server/bash_policy.py`, wired into `hooks/pretooluse.py`. Three outcomes:

1. **hard deny**: `command_touches_secret_path()` scans every token, including
   the value behind `--opt=`, falling back to whitespace splitting on unbalanced
   quoting so a stray quote can't switch the check off. Refused even if Mike
   would approve.
2. **vetted allow, decided locally with no POST**: an allowlisted command (`git`
   read-only subcommands, `ls`, `wc`, `date`) where every argument resolves
   inside the working tree.
3. **prompt**: everything else. A gap here costs latency, not safety, which is
   why vetting is free to be conservative.

**Both allow lists contain no Bash rules at all.** A Bash call with no allow
rule falls through to the hook, and the hook's allow clears it, so an entry in
settings.json bought nothing while costing the boundary.
`test_no_bash_rules_in_the_allow_list` replaces the verb denylist that let `git`
through. Pinned by `test_vetted_command_is_allowed_without_reaching_the_server`
(the hook pointed at a dead port: if it POSTed, the unreachable-server path
would deny).

Things the tests caught while building it: resolve **every** token (a symlink
inside the tree pointing out slipped past an absolute-or-`..` check); compare a
candidate against a root computed **the same way** (a normalised path against a
`realpath`'d root fails everything on macOS, where `/home` is autofs); check each
colon-separated segment (`HEAD:../../.ssh/config` normalises to a path *inside* the
tree); and `git -c core.pager=<cmd>` is execution with a non-path argument, so the
pre-subcommand slot gets an explicit allowlist (`--no-pager`, `--paginate`).

### Review passes: four findings, one structural fact

The four: the `git diff --no-index` verb hole (above), brace expansion, the hook
crash and globs.

- **Brace expansion.** The first version gated vetting on a *denylist* of shell
  metacharacters that omitted `{}`: `wc -l {,}/home/marlow/.ssh/id_ed25519`
  expands to an absolute path while the literal token reads as contained,
  clearing both checks on the one path that skips human review. Fix: an
  **allowlist of characters a token may contain**. `^ % + : ~ @ ,` are on it
  because `HEAD^`, `--format=%h`, `date +%Y` and `HEAD:path` are ordinary usage;
  a charset that prompts on those trains Mike to stop reading prompts. The same
  pass added a newline check against the raw command: `shlex.split()` swallows
  newlines, so `git status\ncurl evil` tokenises to blameless tokens while the
  shell runs two commands.
- **An unhandled exception took the whole hook out.** `Path(v).expanduser()`
  raises `RuntimeError` on Python 3.13+ for an unresolvable `~user`, so a junk
  prefix disabled the hard deny. A crash is not neutral: the CLI treats a
  non-zero hook exit as a non-blocking error and may run the tool. Fixed
  specifically, and with a catch-all in `main()` making "always exits 0" true.
  **Any new code path in the hook needs the same guarantee.**
- **Globs were admitted on a false premise** ("pathname expansion can only
  produce paths"). Containment resolves the literal token; the paths a glob
  expands to are never resolved, so `wc -l *` includes a symlink pointing out.
  Globs are out of the charset (`Glob`/`Grep` are separate path-scoped tools;
  the cost is one prompt). The code comment asserted a protection that did not
  exist, which is worse than no comment.

This module got caught twice by enumerating the dangerous cases, and both times
the fix was to enumerate the safe ones. Assume a third omission exists in
anything still shaped as a denylist.

## Done 2026-08-06: the boundary moves into the kernel (bwrap), this layer is demoted

The four findings share one structural fact: the string `bash_policy.py`
analysed was not the command the shell ran. That is what static analysis of
shell strings costs, and it will keep producing findings. The layer is
misassigned rather than wrong. `invoke.py` is already the single choke point
that spawns the CLI, so it hands the CLI a narrower view of the filesystem than
the server has: `claude -p` inside a bubblewrap namespace where `~/.ssh` and
`~/.marlow` **do not exist**. A kernel namespace does not care about brace
expansion.

**Confirmed 2026-08-06**, `scripts/probe-bwrap-confinement.sh` on the box as
the admin user: CONFINED. Both controls green (the canary is readable without bwrap, and
inside the namespace the CLI still authenticates and runs Bash). One real turn
logged `confined=True`; survives reboot.

- **Ubuntu 24.04 needs `kernel.apparmor_restrict_unprivileged_userns=0`**, or
  bwrap fails at `setting up uid map: Permission denied`. **Settled (Mike,
  2026-08-06): the targeted AppArmor profile**, `deploy/apparmor-bwrap`
  (`deploy/README.md` §11), grants `userns` to `/usr/bin/bwrap` alone and leaves
  the box-wide sysctl at `1`. Acceptance: the probe reports CONFINED *while* the
  sysctl reads `1`.
- **Constraints on `BWRAP_ARGS` (all verified on the box 2026-08-06):** no
  `--unshare-net` (the hook POSTs to the server over loopback from inside the
  namespace; unsharing kills the permission round-trip); `~/.marlow/logs` stays
  inside the tmpfs view the server uses; `~/.claude` stays visible (the CLI
  writes a refreshed OAuth token back every headless run; hiding it breaks the
  subscription auth the CLI-not-SDK constraint exists for).
- **On by default.** A host without `bwrap` fails loudly rather than running
  unconfined; `MARLOW_BWRAP_ENABLED=0` is the explicit opt-out for dev Macs/CI.
  Auto-detecting a missing `bwrap` and silently falling back was rejected: a
  boundary that disables itself where it was never installed is worse than one
  that's off, because nothing says so.
- **What it cannot cover:** `.credentials.json` must stay visible, so it keeps
  its `boundaries.py` guard (now protecting one file). `~/.ssh`,
  `~/.marlow/data`, `~/.marlow/apns` are needed by the *server*, not the CLI.
  Reading a secret only matters if it can leave; `deploy/egress.nft` (Known gap
  #2) is the other half, still deferred on the Cloudflare-rotating-IP question.

**Demoted.** `bash_policy.py`'s docstring now says it is not the secret
boundary: it is latency management, a better error than bwrap's "no such file",
plus the guard behind `credentials_path`. A miss now costs an unnecessary
prompt. **No further hardening passes on that module**; if a fifth bypass turns
up, note it and move on. It keeps its "stay strict" warning because the opt-out
exists and on such a host it carries the whole weight.

## Decisions (Mike, 2026-08-04)

- **Bash: curated verbs only.** `git status|log|diff|show|branch`, `ls`, `wc`,
  `date`. Everything else falls through.
- **Writes: `memory/` unattended, identity files attended.** `SOUL.md` and
  `USER.md` are *absent from allow*, not present in deny: a deny would block them
  in the attended case too, and Marlow rewriting its own character should cross
  Mike's desk, not be impossible.
- **Network: `WebSearch` and `WebFetch` both allowed unattended**, against the
  recommendation to split them (see "Residual risk").
- **Two policy files** (attended vs cron, differing only in `ask`): superseded,
  see "The cron policy".

## Residual risk, deliberately accepted

With `WebFetch` allowed and `Grep` scoped to the whole working tree, an
unattended session can search everything in `memory/` and send it outward.
Secret *paths* are denied at both layers; what's exposed is non-secret-but-
private content. **The control that closes this is `deploy/egress.nft`**
(Known gap #2), deferred over the Cloudflare-IP question. Recorded so the
dependency is explicit.

`git diff`/`git show` can print file contents, including a working-tree `.env`
if one is ever committed there. Kept anyway: the `.env` globs are denied at both
layers, and `--stat`-only would gut a nightly job's ability to summarise its own
changes. Both are "reasonable, not perfect" calls, made knowing the box is
eventually meant to run under NemoClaw / OpenShell. Don't spend more engineering
here than the interim warrants.

## Fixed 2026-08-07: pathless tools (found on the box 2026-08-06)

**Observed:** first smoke test after the path-tool vetting change. `WebSearch`
and `WebFetch` both raised phone prompts though both were in `permissions.allow`;
`ToolSearch` prompted twice. **Cause:** `_decide` branched `Bash` →
`vet_bash_command`, everything else → `vet_file_tool`, and `file_policy.py`
recognised only `Read/Glob/Grep/Edit/Write`. A tool carrying **no path
argument** matched neither vetter and fell through to the phone.

**The lesson, learned three times (Bash 2026-08-05, path tools 2026-08-06,
pathless 2026-08-06): an `allow` entry is inert unless the hook has an
independent reason to agree with it.** Any future allow entry needs a matching
hook path or it is decoration. As found, of 15 allow entries the hook honoured
only 5: `WebSearch`, `WebFetch`, `TodoWrite` and the 8 read-only Gmail/Calendar
tools were decoration, and `ToolSearch` was missing outright.

It blocked dogfood stage 4: unattended there is no prompt to answer, so every
tool the `morning` job needs (Calendar, Gmail, WebSearch) stalled 55 s and failed.

### What shipped

`boundaries.VETTED_PATHLESS_TOOLS`, vetted by `file_policy.vet_pathless_tool`,
tried in `_decide` after `vet_file_tool`, so **strictly after the hard denies**.
It is **pathless by construction, enforced rather than commented**:
`test_vetted_pathless_tools_are_pathless` keeps it disjoint from
`READ_TOOLS`/`WRITE_TOOLS`, and `vet_pathless_tool` abandons vetting if a call
carries a path-shaped key. This vetter runs **no containment check**, so a listed
tool that grew a path argument would be allowed on its name with the path never
resolved; the fix for such a tool is to move it to `file_policy.py`.
`test_pathless_allow_entries_are_honoured_by_the_hook` is the lockstep, asserting
both directions (an allow entry with no vetter is inert; a vetted tool with no
allow entry means the hook is more permissive than the policy file).

### Mike's decision, 2026-08-07: option 2 of three

Chosen: connector reads + `TodoWrite` + `ToolSearch` + `WebSearch` **+
`WebFetch`**. `WebFetch` is genuinely a security call, not a bug fix: it takes
an arbitrary URL and egress is still unenforced. What it removes differs by
context (corrected in review; the first framing, "a phone prompt", was only the
attended half):

| context | before | after |
|---|---|---|
| attended | phone prompt (the only control between a prompt-injected page and an exfiltration GET) | allowed |
| **cron** | **auto-deny** (the hook fires for allow-matched calls and binds, so it fell through to `request_decision`, which with no phone times out and denies) | allowed |

Unattended this is a **lost auto-deny**, and cron is where the exposure is real:
the morning job reads Mike's inbox, so injected text and an outbound GET meet
with nobody in the loop. Backing `WebFetch` out is one line in
`VETTED_PATHLESS_TOOLS` plus the two settings entries. **This makes egress the
mitigation, not a deferral.** The allow list named `WebFetch` from the start but
the hook never honoured it, so this was the first time that entry was live in
either context, not a restoration. `WebSearch` is much narrower (a query to one
backend). `ToolSearch` was an addition:
it loads schemas with no side effect, and the tools it reveals still face the
hook individually, so it grants discovery and not use. Its absence is also the
likeliest reason smoke turn 2 reported Calendar "disconnected" (see
`v2-dogfood-parity.md`, and retest before concluding anything about the
connectors). `TodoWrite` was uncontroversial.

### Reading is not one privilege: `get_thread`/`get_message` excluded

Added 2026-08-07. Mike's line is narrower than "read tools" and predates v2: on
v1 he let Marlow scan the mailbox but **denied `get_thread`**, and wants that
kept. The standing grant is the **Gmail list view** (senders, subjects,
snippets: what `search_threads` returns). Full bodies are a per-occasion call, so
`get_thread` and `get_message` are absent from `allow` and
`VETTED_PATHLESS_TOOLS`, pinned by `test_full_message_bodies_are_not_a_standing_grant`
(they read as an obvious omission a later session would "finish").

**Absent rather than denied**, as with the write tools: absent still reaches the
phone when attended, so Mike can approve one thread without editing policy; in
cron it is the auto-deny. Cheaper than it looks: the morning briefing summarises
a list view (v1 parity), and bodies are the richest prompt-injection carrier on
the box, which compounds with cron `WebFetch`; keeping bodies out stops the two
grants meeting. Write tools stay absent for the original reason (in cron the
fall-through is the auto-deny that stops an unattended job mutating the mailbox).

### Google Drive: prompt, decided 2026-08-07

The stage-3 retest read `marlow`'s CLI transcript and found **eleven
`mcp__claude_ai_Google_Drive__*` tools exposed to Marlow**; Drive had never been
mentioned in this repo. **Mike: Drive access is a permission prompt.** That was
already the behaviour (no Drive rule exists, so calls fall through: phone when
attended, auto-deny in cron), but it was fail-closed **by omission**, and
omission is not a decision anyone can find later. Pinned by
`test_only_decided_connectors_appear_in_the_allow_list` (the JSON) and
`test_undecided_connectors_are_not_vetted_by_the_hook` (the Python), against a
`GRANTED_CONNECTORS` set naming Gmail and Calendar and nothing else.

**The lesson outlives Drive:** dropping `--strict-mcp-config` means the CLI gets
whatever the **account** has connected, not what this repo enumerated. Connect a
fourth connector on claude.ai and its tools appear in Marlow's toolset silently,
with no commit, deploy or log line. The read-only test only constrains rules
that exist, so an unlisted server was invisible to it.

### Still open, same root cause

The `git add` stall: it prompts by design (object-store writes are off
`VETTED_GIT_SUBCOMMANDS`), right when attended and an unattended failure in
cron. Not addressed here.

## The cron policy: implemented 2026-08-07, and NOT as a settings file

The unattended policy lives in code, in the permission round-trip:
`active_turn.py` publishes the in-flight turn's `source`; `invoke.py`'s
`run_claude` sets it around **both** attempts; `/internal/permission-request`
denies immediately when the source isn't attended (no registry entry, no push,
no 55-second wait). It covers every unvetted tool, connector write tools
included: an unattended inbox scan cannot act on what it just read, and the
half-awake-Allow failure mode is gone because there is no prompt to answer.

**Rejected:** passing a different `--settings <cron file>` per invocation. The
files differed only in `ask: ["NotebookEdit", "Task"]`, so it would have bought
two auto-denies while leaving the real problem (an unvetted tool burning the
55 s window and pushing to a sleeping phone before failing closed), and it rests
on an unprobed question (does `--settings` merge with or replace
`~/.claude/settings.json`?).

**Fail-closed direction:** only `ios` is attended. Cron, `system`, `None` and any
source added later are unattended. Two review findings (2026-08-07) made that
true:

- **`WorkQueue.enqueue` defaulted `source` to `"ios"`**, inverting the property
  for the likeliest mistake: a new unattended trigger calling `enqueue(text)`
  would have been *attended* and able to prompt Mike's phone at 3am, every test
  green. `source` is now required (`test_enqueue_requires_an_explicit_source`).
- **The denial told the model Mike had declined**: a false statement about Mike,
  delivered in his own morning digest. The endpoint now returns a `reason` code
  (`active_turn.UNATTENDED_DENY_REASON`) and the hook says plainly nobody was
  prompted, nobody declined, carry on and report what was skipped. The three
  genuinely ambiguous cases (explicit deny, timeout, server unreachable) keep the
  original wording; all three involved a real prompt.

A cron job can no longer be supervised interactively: an unattended denial is
unconditional.

**`deploy/marlow-settings.cron.json` was deleted (Mike, 2026-08-07).** The case
for keeping it was defence-in-depth for a box where the hook was never wired
(gap #7), but an unread security-relevant file drifts, and this one had spent
three days claiming a blast radius it did not have.
`test_there_is_exactly_one_policy_file` fails if a second `marlow-settings*.json`
reappears: that means someone is re-implementing the unattended policy in JSON,
where the CLI's allow-vs-ask precedence is the thing gate #1 had to probe. The
`allow`-vs-`ask` precedence question is retired unanswered (nothing writes an
`ask` block). `Write` was deliberately never put under `ask`: an `Edit` rule
covers all file-editing tools, so listing `Write` would have gambled nightly
consolidation's memory writes on the unverified precedence (`/code-review
medium`, 2026-08-04). The notebook tools are not vetted either: nothing on this
box uses notebooks, and a vetted-allow is a standing grant, so vetting them would
widen the unattended surface for no use case.

## Found 2026-08-23: five `Write(...)` denies and two allows are inert (FIXED 2026-09-06)

The CLI itself (2.1.221) printed, unprompted, on an ordinary `claude -p` run as
`marlow`, a warning per rule:

```
Permission deny rule: Write(/home/marlow/.claude/**) is not matched by file
permission checks — only Edit(path) rules are. Use Edit(/home/marlow/.claude/**)
instead (Edit rules cover all file-editing tools).
```

Six rules were named: the five `Write(...)` denies covering `AGENT_CONFIG_PATHS`
(`.claude.json`, `.claude/**`, `.mcp.json`, `claude-assistant/.claude/**`,
`claude-assistant/.git/**`) and the `Glob(...)` allow. `Grep(...)` is the same
shape and drew no warning, a reason not to trust the warning list as exhaustive.

**Nothing was exposed, by redundancy rather than design:** every warned
`Write(...)` had an `Edit(...)` twin in the same deny block. This is the fourth
time the lesson landed (Bash, path tools, pathless tools, now denies): *an entry
is inert unless something independently agrees with it.* What is new is that it
applies to **denies**, where the failure is silent in the dangerous direction:
an inert allow produces a spurious prompt that announces itself, a dead deny
produces nothing at all.

**Fixed 2026-09-06.** All seven dead entries are out of
`deploy/marlow-settings.json`; `tests/test_marlow_settings.py` asserts `Edit(...)`
alone plus a guard that each `Write(...)` twin stays absent. Behaviourally a
no-op, because `file_policy.py`'s docstring is the actual argument: the hook is
consulted for allow-matched calls too and its decision wins, and it vets
`Glob`/`Grep` by path on its own. The precondition was met: Mike pasted the live
warnings on 2026-09-06, the same six, no new ones. `Grep(...)` was removed anyway.
**Re-read the warnings after the next CLI upgrade.**

**Not yet checked:** whether the warnings go to stdout or stderr. `invoke.py`
parses `--output-format json`/`stream-json` off stdout, so if they ever land
there they sit in front of the payload. Nothing has broken, which suggests
stderr.

## Found 2026-08-23: Marlow misreports its own capabilities, because tools are deferred

The second occurrence (the first, `boundaries.py`: Marlow reported Calendar
"disconnected" on smoke turn 2). This time it told Mike it had no "live web
search wired up", then searched the web successfully when asked to search CBC.

**Cause.** The deployed CLI defers most tools behind `ToolSearch`. A probe as
`marlow` returned only: `Agent Bash Edit ListAgents Read ReportFindings
ScheduleWakeup Skill ToolSearch Write`. Asked *about* a capability, Marlow reads
a list that lacks it and answers honestly and wrongly; asked to *use* it, it
calls `ToolSearch`, finds it, and works. Neither is a server bug.

**Fixed on the box, not here.** A four-line `## Tools` section in
`/home/marlow/marlow-identity/CLAUDE.md` says the visible list is partial and to
check `ToolSearch` before saying it cannot do something. That file is in the
private identity repo, so it is invisible from this one.

**Rejected: eagerly loading `WebSearch`.** No documented switch on 2.1.221
(`--tools` selects which built-ins exist, not which load eagerly; a per-server
switch for MCP tools arrived later, and weather uses it), and it would be a
per-tool patch for a general problem. The behavioural rule costs one line of
per-turn context instead of a schema on every turn, cron included.

**Confirmed 2026-08-24:** asked to enumerate its tools verbatim, Marlow returned
the same ten eager tools but now goes to `ToolSearch` first. So a resumed
session *does* pick up an edited cwd `CLAUDE.md`; the freeze in
`cache-friendly-identity-injection.md` applies to the injected identity string
only. No wait for the 01:00 rollover was needed.

**Batching is a startup race, not a property** (corrected within the hour). Marlow
saw the deferred list arrive in three batches and the first draft concluded the
surface "materialises as the servers connect" and no static list could cover it.
**Wrong:** a box probe that evening showed one turn, no `ToolSearch` call, the
complete connector set listed up front; the batching was that session's MCP
servers still connecting. The wrong version argued against exactly the mechanism
that then worked (see `marlow-tool-surface-trimming.md`).

**Not an exposure, checked.** The listing includes every Gmail/Calendar/Drive/
Trello *write* tool plus the harness's scheduling and messaging tools. None is
granted: they appear in neither `VETTED_PATHLESS_TOOLS` nor
`marlow-settings.json` (grepped 2026-08-24 for `Cron`, `RemoteTrigger`,
`PushNotification`, `SendMessage`, `Agent`, `Task`, `Drive`, `Trello`,
`Excalidraw`, `Mermaid`: zero hits), so each falls through to `request_decision`:
phone prompt when attended, auto-deny in cron. **Visibility in a `ToolSearch`
listing is not a grant.**

**Found 2026-08-24: `TodoWrite` was a dead grant.** In `permissions.allow` and
`VETTED_PATHLESS_TOOLS` but absent from every tool listing at 2.1.221, including
the baseline. Harmless, but an entry nobody has checked is an entry nobody can
reason about. Removed from both halves together 2026-09-06 (the lockstep test
fails if only one goes).
