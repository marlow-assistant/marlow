# Task — what git setup (if any) should `marlow`'s working dir start with

**Status:** shipped. Scoped 2026-08-08 (design pass with Mike, "Resolved
2026-08-08" below); pieces 1-3 built and merged 2026-08-08; piece 4 (the pusher)
built 2026-08-09, so the brain is off the box. Opened 2026-08-04 as a placeholder.
Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

## What prompted this

On-device testing of `ios-permission-round-trip-ui.md` surfaced that Marlow's
working dir (`/home/marlow/claude-assistant` on the box) was a local-only repo:
branch `master` (not `main`), no remote, its own history starting from a
`seed: initial identity` commit followed by memory commits.

**Corrected 2026-08-08: this was not the LLM improvising.** The first writeup
concluded Marlow ran `git init` itself. It didn't: `deploy/seed-from-v1.sh` does
exactly that at seed time, with no `-b main` and no `git remote add`, so
`master`-with-no-remote was the script's designed output. **That matters for where
the fix goes:** it is a gap in provisioning, not a behaviour to constrain.

Assessed as benign at first (nothing in `server/` depended on git state there),
but a real gap: the seed script versioned the brain locally and called that
recoverable, which is true of a bad *edit* and false of losing the machine. The
private identity repo existed for exactly this and was never wired up. The
harness-owned auto-commit "expected per Mike" is recorded in `marlow-v2-START.md`'s
hard constraints ("Git in the identity dir is harness-owned, not Marlow's
discretion") and had never been built.

## Resolved 2026-08-08: the design pass (Mike, in chat)

Prompted by Mike asking whether to commit two hand edits to `cron/morning.md` on
the box, which surfaced that the working dir had never been committed beyond
`memory/`.

### What exists (established 2026-08-08)

- **v1 repo** (private, `main`): **frozen**, last commit the morning v2 went
  live (v1's final nightly memory commit).
- **The legacy user's clone of it**: live `origin` and a write deploy key. The one
  remaining path that can still push to v1.
- **The identity repo** (private, tiny, 5 commits all 2026-07-24): the authored v2
  SOUL/CLAUDE/USER. The intended home, never wired up.
- **`~marlow/claude-assistant`**: the live brain. Local-only `master`, two days of
  production state, one copy, one machine.
- **`marlow-server/identity/`**: blank template skeleton, correct as-is.

The three-role split from `marlow-v2-repo-structure.md` was executed correctly
(v1 untouched, software ff-only, identity diverging freely). Exactly one step was
missed: nothing connected the diverging working dir to the repo created to hold it.

### Decisions

1. **Remote is the identity repo**, not the v1 repo: the entire reason for the
   split was to stop a diverging brain pushing into Mike's live v1.
2. **Keep the box's history; merge, don't replace.** The box's SOUL/CLAUDE/USER
   were `cp`'d from the identity repo by the seed script, so a one-time
   `merge --allow-unrelated-histories` is conflict-free: one history holding the
   authoring commits and the live divergence. Then `master` → `main`, then push.
   (Done; the hand merge was one-time.)
3. **~~`marlow` never holds a push credential, a root-owned pusher does.~~
   REVERSED 2026-08-09: the key lives in `~marlow/.ssh/identity-push`.** The
   original reasoning was unattended durability without a push credential in reach
   of an agent with shell access. `/security-review` found three root-execution
   paths through the config allowlist that made that safe; **build-order piece 4
   records the whole reversal and the decision rule**; do not re-derive this
   from the paragraph it replaced. What survives: the server commits (as `marlow`,
   purely local), **Marlow-the-LLM** does neither the commit nor the push, the
   script is root-owned and marlow-executed, and the integrity risk the root
   design was reaching for is carried by `scripts/backup-identity-bundle.sh` on
   the Mac. It needs its **own** keypair (GitHub rejects a deploy key already used
   on another repo), unpassphrased.
4. **Auto-commit is server-side, post-turn**, in the work-queue path after
   `invoke.py` returns, **not** a CLI `Stop` hook: the server owns the turn
   boundary, the queue already serializes everything so there is no race, and it
   stays in tested Python rather than shell inside the scrubbed-env child. No-op
   guard when clean; a commit failure logs without failing the turn (durability
   must not break conversation). Git *writes* are denied to the model in
   `boundaries.py`/`bash_policy.py`; read subcommands stay allowed.
5. **Commit messages are mechanical and carry no prompt text.** Deriving
   v1-style semantic messages from the turn would put user content in git history
   (which must never hold it); the file content is the semantic
   record anyway. And once git writes are denied to the model **no
   model-authored commit message is possible at all**, including the nightly
   consolidation turn. So the readable daily record has to be a *file* the
   consolidation job writes under `memory/`, which the harness commits
   mechanically. Don't design a summary-commit path; it would be denied.
6. **Push cadence: nightly** originally (max loss one day); built as hourly at :17.
7. **`seed-from-v1.sh` gains an optional `--origin <url>`** (and `init -b main`
   unconditionally), so the next install isn't stranded the same way. The real fix
   lives here: the stranded repo is provisioning output, not agent behaviour.

### Hard requirement on the auto-commit: it must not commit secrets

Raised by Mike 2026-08-08 ("any other drawback?"); the one that turns this design
into a leak if missed. **`git add -A` bypasses `SECRET_GLOBS`.** That protection
lives on the tool-call path (`reaches_protected_path`); a server-side commit is
not a tool call, so nothing in `file_policy` sees it. Gate #2 found a **real**
`.env` in the working tree holding a live token; per-turn `-A` would commit it
and the push would put it on GitHub. Same shape as the 2026-08-04 leak: a secret
sitting inside a value some *other* machinery copies.

The commit path must exclude anything `SECRET_GLOBS` matches **in code, not by
relying on the working dir's `.gitignore`**: a gitignore is inside the tree the
agent can write, one hand-edit from not covering the case. A `.env` in the tree
must survive a turn uncommitted, and nothing matching may already be tracked
before the first push (`.gitignore` does not untrack).

### Two consequences

**The working dir's `.gitignore` is now load-bearing, not hygiene**: per-turn
`git add -A` would otherwise recommit generated board logs on every turn after a
sync. A personal log that `cron/morning.md` reads must stay tracked, since it is
Mike's data. **Per-turn auto-commit makes the 2026-08-07 hole legible:** Marlow may
edit its own `scripts/` and `cron/*.md` behind a phone prompt, and every such edit
is now a diffable commit after the fact. The permission gate and the audit trail
come from different places, the right shape.

### Recommended, not decided

**Archive the v1 repo on GitHub.** Read-only v1 removes the accidental-push path;
the legacy clone's live `origin` and write key leave it open today. Reversible.

## Resolved 2026-08-07: Marlow-authored scripts stay editable, gated by a prompt

Raised planning the `trello-sync` port (`v2-dogfood-parity.md`). Mike's context is
the whole argument: `trello-sync.sh` was written by **Marlow v1, not by Mike** (he
was talked through creating a dev key and the assistant wrote the script, and has
updated it since). So authoring and maintaining its own tools is not incidental to
what Marlow is. "Marlow may never edit a script" was never the requirement;
**"Mike should know when it wants to"** is.

Needed no work: `file_policy` vets *writes* only under `memory/` and *reads*
anywhere in the tree, so a write to `scripts/`, `cron/*.md` or `SOUL.md` falls
through to the phone, and running a script prompts too. Keep the rule in that
shape, *"`memory/` is the only thing Marlow rewrites without asking"*, rather than
a `scripts/`-specific rule: a new directory inherits the right behaviour by
default, where an enumerated rule must be remembered. This repo has been bitten by
enumeration three times (Bash verbs, path tools, pathless tools).

**Rejected: a promotion step for Marlow-authored scripts** (source in the tree,
root-owned copy under `/opt/marlow/`, synced by `update.sh`). Put to Mike as one of
two options on 2026-08-07; he rejected both. Requiring an `update.sh` run for
every script change makes Marlow's tool-building contingent on Mike being at a
terminal, hamstringing the capability the port exists to preserve. So
`trello-sync` and its like live in the working tree, editable with a prompt, with
no `/opt` copy. `entrypoint.py` stays root-owned: it is harness, not a
Marlow-authored tool, and it runs *every* job.

**The accepted hole, stated plainly.** A script Marlow edits today may be run by
cron tomorrow with nobody watching, and the only control is a phone prompt
rendered as a ~100-char one-liner. Mike's call: *"I'm ok with some holes that
would get plugged by that eventual environment"* (NemoClaw / OpenShell is the
intended jail; the boundaries here are an interim). What shrinks the hole is the
red/green diff in the approval UI (`ios-permission-round-trip-ui.md` → "Future"):
the gap is *legibility*, not permission.

## Resolved 2026-08-04: the exposed token, and the credential pattern it settled

Checking the legacy user's clone for comparison, `git remote -v` printed a live
GitHub personal access token in plaintext (embedded in the `origin` URL) into a
Claude session transcript. Done: the token was deleted outright, and the clone now
authenticates over SSH with a dedicated unpassphrased keypair registered as a
**repo deploy key** with write access (not the account-wide SSH keys list), bound
**per-clone** with `core.sshCommand ... -o IdentitiesOnly=yes`. A blanket
`Host github.com` block would silently capture a *second* GitHub key on the host
later, which is what `marlow` getting its own remote would create.

Three inputs to the design:

1. The credential no longer lives in any string git prints. The leak wasn't a weak
   secret, it was a secret stored inside a *display* value.
2. `~/.ssh` is in `SECRET_PATHS`, so the hook denies reads before waking a phone;
   `.git/config` is inside the working tree the agent reads freely, so a token
   there can't be covered without special-casing a path *within* `working_dir`.
3. A deploy key is per-repo and cannot be reused, so `marlow`'s working dir needs
   its own keypair and deploy key entry.

Unpassphrased is deliberate: on an unattended box a passphrase either sits in an
agent unlocked at boot (no gain) or blocks every push, which would break the
harness-owned auto-commit design in the START doc's Known gap #3.

## Build order

1. **DONE: git writes denied to the model.** `bash_policy.command_is_git_write`,
   via `hooks/pretooluse.py`. **Implemented as an inversion of
   `VETTED_GIT_SUBCOMMANDS`, not the `DENY_ARG_SHAPES` extension this doc first
   called for.** A hand-written list of write verbs omits `stash`, `apply`,
   `cherry-pick`, `worktree`, `filter-branch` and whatever git adds next, and
   `bash_policy`'s history is two denylists that each omitted a case. Inverting
   the read allowlist means a new subcommand is denied until someone decides it is
   a read, the direction the mistake should fall. `DENY_ARG_SHAPES` keeps
   `git push --force` as defense in depth. Cost: `git help <x>` is denied rather
   than prompted, accepted. It matches `git` in **command position** (token 0,
   after shell punctuation, or after a wrapper like `env`/`xargs`) and
   basename-matches; *not* every `git` token, which was the first cut and
   hard-denied `grep -n git memory/notes.md`. Global options that take a separate
   value consume it, or `git -C memory log` reads `memory` as the subcommand.
2. **DONE: server-side post-turn auto-commit.** `server/identity_git.py`, called
   from `queue.py`'s drain loop via `asyncio.to_thread`, outside the invocation
   try/except and unconditional on turn status (an ERRORed turn can still have
   written files). Secrets excluded **by construction** (never staged, rather than
   staged and cleaned up) plus a belt-and-braces index re-check that aborts rather
   than commits a secret; `AGENT_CONFIG_PATHS` excluded too. `--no-verify` because
   `.git/hooks` is inside the tree but outside `WRITABLE_SUBTREE`. Staging uses
   `:(literal)` pathspecs (pathspecs are wildmatch patterns and Marlow can create
   `memory/*` with no prompt), and runs **after** the TURN_READY push so a stalled
   git cannot hold back a reply. Ignored paths under `memory/` are **reported**:
   silence would mean memory quietly stops being backed up, indistinguishable from
   a turn that changed nothing.
3. **DONE: `seed-from-v1.sh --origin` + `init -b main`.** Sets the remote only;
   never pushes, installs no credential.
4. **DONE: the pusher, running as `marlow` + an off-box bundle.**
   `deploy/identity-push.py` from `marlow`'s crontab hourly at :17, deploy key at
   `~marlow/.ssh/identity-push`; plus `scripts/backup-identity-bundle.sh` run
   **from Mike's Mac** (`deploy/README.md` §7a/§7b). Tests use a real bare remote,
   so the non-fast-forward rejection is exercised, not asserted about. **A
   rejected push is reported, not swallowed**: only the box pushes, so the sole way
   to diverge is editing the identity repo through the GitHub web UI, and a cron
   failing silently would stop backing up the brain unnoticed. Treat that repo as
   write-only-from-the-box.

   **This piece was built as a ROOT-owned pusher first, and that design was
   reverted.** The reversal is the most valuable thing the branch produced.

   The premise was "no push credential in `marlow`'s reach": key at
   `/etc/marlow/identity-push`, root:root 600, fired from root's crontab. Making
   that work required a root-owned `/etc/marlow/gitconfig` with `safe.directory`
   via `GIT_CONFIG_GLOBAL`, i.e. **switching off the guard git added for exactly
   this scenario** (CVE-2022-24765), then re-implementing the safety as a config
   allowlist. `/security-review` took that allowlist apart: three root-execution
   paths, verified against real git 2.50.1, each running **while the tamper
   precheck reported clean**:

   - `remote.<n>.receivepack` is **first-value-wins**, so a repo-local value beats
     `-c`; only `--receive-pack=` overrides it. Worst: with a local-path URL the
     push exits **0**, the pusher prints `ok`, and no alert fires (silent hourly
     root execution).
   - `protocol.ext.allow = always` + an `ext::` URL: key *specificity* means a
     repo-local `protocol.ext.allow` beats a command-line generic `protocol.allow`.
   - `core.gitProxy` **accumulates**, so `-c core.gitProxy=` does not displace it.

   All three needed a write primitive into `<working_dir>/.git/config`, which
   `AGENT_CONFIG_PATHS` denies, which is why none scored above confidence 7. But
   that is the layer this doc had to add after the layer before it failed
   (`git log --output=`, 2026-08-08), and the blast radius had moved from the
   server process to root.

   **The decision rule:** running a privileged process over a directory a
   less-privileged agent can write is an escalation antipattern when the data is
   config-as-code, and `.git/config` is exactly that. Three review passes each
   found bugs in code the previous pass wrote and the security pass found the
   premise leaking; that is a signal about the design, not the care taken.

   **And what was being bought was narrower than it looked.** Exfiltration was
   never the risk: `invoke.py` omits `--unshare-net` (the hook needs loopback) and
   egress is deferred, so the agent already has open internet and a repo-scoped
   deploy key adds no channel `curl` lacks. The real risk is **backup integrity**
   (the agent force-pushing away the thing that survives the agent misbehaving),
   which is about write semantics on the remote, not where the key lives. Paying
   root-RCE exposure for it was a bad trade. **Mike's call, 2026-08-09: give
   `marlow` the key, and carry the integrity risk with the bundle.** No tamper
   precheck, no `safe.directory`, no hardening flags, no sudo.

   **Corrected 2026-08-09** (`/security-review`, sub-threshold 6/10, reported
   because it invalidates a rationale). The first version said the three findings
   *evaporate* because hostile config now runs as `marlow`, "which the agent
   already is". Wrong: `invoke.py` mounts a tmpfs over every `SECRET_PATHS` entry
   for each `claude -p`, so `~/.ssh` (the write-capable key included),
   `~/.marlow/data` and the APNs dir are kernel-hidden from the agent, while the
   pusher runs from `marlow`'s crontab outside that namespace. The findings
   **downgrade** (root-RCE to confinement escape), they don't disappear. What
   holds them is that `<working_dir>/.git` is in `AGENT_CONFIG_PATHS`, the pusher
   now its second consumer alongside `identity_git.py`. **Decision rule: the next
   write primitive found into `<working_dir>/.git` hardens the pusher in the same
   change that closes it** (precheck, or `-c` pinning of the keys above, chosen
   against what the primitive reaches). Not built now: one denied path, no
   measured failure. **Read the cron and the bundle as one mechanism**: the cron
   is convenience, the bundle is what makes a write-capable key acceptable.

   Pull-based replication (a puller elsewhere fetching and pushing with
   `--ff-only`, no credential on the box) was the considered alternative and is
   strictly stronger on integrity. Not chosen: it makes replication depend on a
   travelling laptop being on, and the bundle already covers the loss case.
   Revisit if the box ever holds something the bundle cadence is too slow for.

   **Two things kept from the root design:** *Root-OWNED, marlow-EXECUTED*: the
   script installs `-o root -g root -m 755` to `/opt/marlow/bin/` (same rule as
   `deploy/README.md` §6b: a crontab line into Marlow's own tree would let the
   agent rewrite tomorrow's scheduled job). And **`tampered_config_keys` was
   fail-OPEN**: it returned `[]` on any non-zero exit, commented as git's "no
   matching keys" (exit **1**), so a stalled git or unreadable `.git` reported a
   clean repo *without reading a key* (found because root in a `marlow`-owned repo
   gets exit **128**). Now raises `GitConfigUnreadable`, and `commit_after_turn`
   treats it as a refusal. **"Reused, therefore already correct" is not a
   property**: a helper's error handling encodes its original caller's assumptions.

### Found by /security-review: the sink made a dormant primitive live

`/security-review` found **RCE as the server process**, confirmed end to end, the
most instructive finding of the piece. `.git/config` names commands git
*executes* (`core.fsmonitor` on `status`, `filter.<n>.clean` on `add`), neither
covered by `--no-verify`. And the config was writable with **no human in the
loop**: `git log --output=<path>` is a vetted *read* that writes arbitrary bytes
anywhere containment allows, and `--format=%x##` synthesizes `[`, `]`, `=`, `"`
and newlines using no character outside `VETTED_TOKEN_CHARS`. One auto-approved
command wrote the config; the next turn's harness commit ran it, as the process
outside the bwrap namespace that sees every secret the namespace hides.

**The primitive and the unprotected `.git` both pre-existed on `main`, inert
because nothing privileged ran git in that directory.** This piece supplied the
sink. The rule: adding a privileged execution path over a directory the agent can
write turns every dormant write primitive live, so the question for a new sink is
not "is this code safe" but "what already writes here."

Fixed in two layers: (1) **`<working_dir>/.git` added to `AGENT_CONFIG_PATHS`**,
the primary control, closing every write primitive into that directory at once
(`log`/`diff`/`show --output=` and whatever is next) because they all pass the
same `reaches_protected_path` check on the option value; (2) **`identity_git._git`
passes `GIT_HARDENING_FLAGS`** (`-c core.fsmonitor= -c core.hooksPath=/dev/null`)
on every call and refuses to run if `tampered_config_keys()` finds a `filter.*` or
`include*` key (open-ended names `-c` cannot neutralise; nothing here sets them
legitimately). The one place the module fails **closed**: a missed backup is
recoverable, executing an agent-chosen command is not.

**The fix itself then drifted.** `AGENT_CONFIG_PATHS` is mirrored by hand into
`deploy/marlow-settings.json` (deliberately: gap #7 was a box where the hook was
never wired), and adding `.git` left that file behind. There was a drift guard for
`SECRET_PATHS` and none for `AGENT_CONFIG_PATHS`; there is now. The mirror covers
the `Write`/`Edit` half only and **cannot** express the primitive actually found,
since `git log --output=` is a Bash call and Bash rules there are prefix matches.

**Deliberately NOT done: anchoring the relative token in
`command_touches_agent_config`** so `.git/config` earns a hard deny. That function
is the unapprovable path and is fed raw strings, so `echo ".git/config"` would
become undeniable-and-unapprovable, the false-positive class `/code-review` had
just found in `command_is_git_write`, and the call `bash_policy` made for `.env`
on 2026-08-06. Prompt is right: attended, Mike refuses; unattended, `active_turn`
auto-denies anything that would prompt.

### Found while building: the drain loop was trusting a docstring

`commit_after_turn` is documented as never raising, and a test asserting the queue
survives it raising anyway **failed**. An exception escaping killed the single
consumer task, after which every later turn sits `PENDING` forever with no error
(the orphaned-turn shape of the reauth gap in `queue.py`). Now guarded at the call
site. The rule: this queue has one consumer, so anything newly awaited in
`_drain` needs its own guard regardless of what the callee promises.

### Asked and dismissed: renames

Mike asked 2026-08-08 whether harness-side commits handle renames worse than the
LLM running `git mv`. They don't: git stores **content trees, not renames**;
detection is a read-time similarity heuristic, and `git mv` is `mv` plus staging
both paths. Per-turn commits are *finer*-grained than model-discretion ones, and rename
detection degrades when a move is bundled with heavy edits, so this is marginally
better. Don't reintroduce `git mv` for the model on these grounds.

### Sequencing note

Git writes denied to the model must not outlive the box's CLAUDE.md instructing
Marlow to commit its own memory, or every memory write ends in a denial. The
harness commits before the model is denied, but **that instruction has to be
pulled from the box's CLAUDE.md** when this deploys (an edit in the identity repo).
