# Marlow v2 — Opus Handover: Repo Structure Review

**Date:** 2026-07-18
**Purpose:** Mike wants an Opus review of a structural concern that surfaced
during this session, before more gate #4+ work gets built on top of it. This
doc is the handover — recap of what happened, then the concern in Mike's own
terms, then pointers to read. Not a request to implement anything yet; a
request to review and help decide.

## What happened this session (Sonnet, on Mike's dev laptop)

1. **Gate #2** (permission audit) was already done in an earlier session (SSH'd
   into the Marlow server directly) before this one started — see
   `docs/marlow-v2-gate2-results.md`. This session just pulled and confirmed it.
2. **Gate #3** (server skeleton) was built this session, entirely from Mike's
   *dev laptop*, not the actual Marlow server:
   - All of `server/` — `config.py`, `boundaries.py`, `log.py`, `invoke.py`
     (the only module that shells out to `claude -p`), `queue.py`,
     `auth_monitor.py`, `pairing.py`, `push.py` (stubbed), `health.py`,
     `app.py` (public tailnet + internal loopback ASGI apps).
   - A 34-test pytest suite (`tests/`) — added mid-session at Mike's request
     ("I get leery when there aren't tests"), replacing ad-hoc curl
     smoke-testing. Caught three real bugs by actually running the code
     against a fake `claude` binary (not just import-checking): `/v1/pending`
     had ready/pending backwards, the work queue's `asyncio.Queue` was bound
     to an event loop at import time instead of inside `start()`, and the
     pairing admin flow was originally a disconnected CLI script that could
     never share state with the running server.
   - `deploy/` artifacts: systemd unit, watchdog, dead-man's switch (for a
     different host), egress.nft (untested against real interfaces).
   - Full writeup: `docs/marlow-v2-gate3-results.md`.
3. **Post-gate-#3 pivot** (still this session, after Mike pushed back): the
   deploy model initially assumed the new `marlow` systemd user would need
   read access into `mike`'s home (`~/claude-assistant`, `~/.claude/.credentials.json`)
   to reuse the existing assistant's config/memory/auth. Mike corrected this:
   v2 should be standalone. Landed on: `marlow` gets its own `git clone` of
   **this same repo** (a one-time seeded copy of `CLAUDE.md`/`SOUL.md`/
   `USER.md`/`memory/`, diverging independently from there) and its own
   interactive `claude` login (own `.credentials.json`). Also resolved plan
   §8's other open item: Marlow v1 (tmux + Discord) gets **retired** once v2
   + the iOS app are proven — not a permanent two-Marlows setup.
   `deploy/README.md`, `marlow-server.service`, and a new `deploy/update.sh`
   (manual fetch/ff-merge/reinstall/restart/health-check script) reflect this.

All of the above is committed and pushed to `main`.

## Mike's concern (his words, lightly trimmed)

> The bootstrapping of Marlow v2 from the existing stuff I think kind of
> 'lost' the "Marlow v2 is installable software" angle that I would like —
> the bootstrapping was important, but now I worry we've got a single repo
> doing two things — being Marlow v1 as my assistant, and being a repository
> for Marlow v2 which is a separate piece of software.

Unpacking that a bit, as I (Sonnet) understand it: this repo currently serves
two different roles at once, and the "standalone, seeded-copy" fix decided
above **papers over that rather than resolving it**. Specifically:

- **Role 1 — Mike's running assistant's brain.** `CLAUDE.md`, `SOUL.md`,
  `USER.md`, `memory/*.md`, `scripts/*.sh`, `logs/*.md`. This is personal,
  operational, changes constantly (every conversation potentially writes to
  `memory/`), and its git history is really an activity log of the assistant's
  life, not a software project's commit history.
- **Role 2 — Marlow v2's actual source.** `server/`, `tests/`, `deploy/`,
  and the `docs/marlow-v2-*.md` design/gate docs. This is a real piece of
  software now: a Python package with its own `requirements.txt`, its own
  test suite, its own deploy lifecycle (`systemd`, a dedicated user, an
  update script). It has version-to-version concerns (does this deploy
  break the last one? what's the rollback?) that have nothing to do with
  Role 1.

The "seeded copy" mechanism landed on this session — `marlow` does
`git clone` of *this repo* — technically resolved the immediate
cross-user-permissions problem, but it does so by making v2's deploy
**depend on cloning the same repo that is also Mike's live assistant
memory**. Every future `git pull` on the Marlow-v2 side pulls in whatever's
sitting in this repo at that moment, mixing "a code change to `server/`" and
"today's memory-file commits" in the same fetch. That's the thing Mike is
flagging as unresolved, not a settled decision.

## What's worth Opus's judgment here (not pre-decided — genuinely open)

- Should `server/`/`tests/`/`deploy/` (Role 2) move to their **own repo**,
  separate from the assistant's memory/identity repo (Role 1)? That would
  make "Marlow v2 is installable software" literally true (clone *that* repo
  to deploy it, independent of Mike's personal assistant state) but loses the
  "seeded copy falls out of a single `git clone`" convenience — the
  CLAUDE.md/SOUL.md/USER.md/memory/ seed would need an explicit, designed
  hand-off (a one-time export script? a template with placeholders? something
  else?) instead of just being already-there.
- Or is the concern more about **git hygiene within one repo** (e.g., tighter
  `.gitignore`, clearer directory-level ownership, decoupling commit
  cadences) rather than a full repo split?
- Either way: this changes `deploy/README.md` §0's install steps and
  `deploy/update.sh`'s fetch semantics, so it's worth settling before gate #4
  builds more on top of the current layout.

## Where to look

- `docs/marlow-v2-implementation-plan.md` — the self-contained build plan
  (§2 module layout, §5 security boundaries, §7 build order).
- `docs/marlow-v2-gate3-results.md` — what got built this session, including
  the standalone-architecture pivot at the bottom.
- `deploy/README.md` — the current (just-revised) install story this
  concern is about.
- `memory/projects.md` — Marlow v2 entry, status log across all three gates.
- `docs/marlow-v2-design.md` — rationale record only; §9 has the earlier
  NemoClaw/OpenShell "standalone but shaped for later adoption" thinking,
  which is adjacent to this same repo-boundary question but about a
  different boundary (this host vs. a future sandbox host, not this repo vs.
  a software repo).

Nothing needs to be built differently right now because of this — gate #3's
code itself doesn't care which repo it lives in. This is purely about
deciding the repo/deploy story before gate #4 (permission round-trip) and
gate #6 (iOS app) add more surface that would also need to move if a split
happens later.

## RESOLVED 2026-07-19 (Opus review + Mike) — full repo split

Decision: **split into two repos**, and treat Marlow v2 as shrink-wrap
software whose *identity is deploy-time data, not baked-in source*.

**Why (the deciding fact, not just aesthetics):** the current single-repo
deploy has a latent, guaranteed failure. `~/claude-assistant` on the box is
at once the software source, the `claude -p` working dir, AND the brain that
CLAUDE.md's write rule commits+pushes every conversation. `deploy/update.sh`
ships code via `git merge --ff-only`. The moment the deployed `marlow` writes
one memory file it creates a local commit → either it pushes divergent
personal memory into Mike's live v1 `origin/main`, or the next `update.sh`
ff-merge fails. A single git branch cannot be both fast-forward-updatable
(code shipping) and independently-diverging (a living brain). No `.gitignore`
/ hygiene fix resolves this; the split does.

**Shape:**
- **`marlow-server`** (new repo, Role 2) — the shrink-wrap product: `server/`,
  `tests/`, `deploy/`, `docs/marlow-v2-*.md`. Contains **no** personal
  SOUL/CLAUDE/memory. Ships an empty `identity/` skeleton (structure, no
  content) so a fresh install boots as a blank Marlow. History: **fresh repo,
  clean start** (first commit = current gate-3 tree; design docs move as files,
  the personal/memory commit history does not come along).
- **`claude-assistant`** (this repo, Role 1) — stays Mike's live v1 assistant,
  untouched. Also the (optional) seed source for a v2 instance's identity.
- **On the box, two locations that stop fighting over one git history:**
  software at `/home/marlow/marlow-server` (ff-only `update.sh` target, never
  diverges); identity/working dir at `MARLOW_WORKING_DIR`
  (`~/claude-assistant`) seeded **once**, then diverges freely, never touched
  by updates. `config.py` already separates these — today they're just forced
  into one directory.
- **Seed is a deploy step, not a dependency:** `deploy/seed-from-v1.sh`
  (optional, one-time) copies SOUL/CLAUDE/memory from a v1 checkout into a new
  instance's working dir. Skip it → clean-slate Marlow. Never writes back into
  Mike's `origin/main`.

Bonus: server code moves *out of* the `claude -p` working tree, so the agent
can no longer edit its own server source.

Execution is a repo-reshaping, **near-zero code change** (gate-3 code and the
CLI contract are unaffected; only `deploy/README.md` + `update.sh` get
rewritten for the two-location layout). Do it before gate #4.
