# Dev workflow — "Mike + Claude, banging out software"

Decided 2026-07-19. This is the operating agreement for how changes land in
`marlow-server`. It's tuned for a **single human writer + an AI writer**, private
repo, high velocity, wanting a green trunk without PR ceremony. If a second human
ever writes to this repo, revisit — some of these tradeoffs change.

## The shape: trunk-based with a fast gate

- **`main` is the trunk** — always green, always deployable.
- **Trivial changes** (docs, a config tweak, a typo) → commit on `main`, no
  branch or review. Don't gate yourself into misery over a README edit.
- **Non-trivial changes** (real code) → short-lived branch → local suite →
  automated review → merge. See "the flow" below.
- **Commit and push branches freely; ask Mike before opening a PR or landing
  anything on `main`** (a merge or a direct push — trivial commits included).
  That OK is a release decision, not a code review: Mike is **not**
  diff-sniffing line-by-line like a tech lead. The review that matters is done
  by **tools**, not his eyeballs (see below).
- **Get uneasy the further you drift from `main`.** No WIP limit — but in-flight
  work is inventory, and inventory hides defects. Two unmerged branches should
  raise an eyebrow; a branch based on another branch should raise both. Getting
  back to trunk regularly is the goal; how, on any given day, is your call.

  The failure to remember is quiet. 2026-09-06, five PRs merged with one chained
  command: `gh` printed ✓ five times and two of them landed in their stacked
  base branches instead of `main`, because GitHub retargets a stacked PR
  asynchronously. Every PR still read MERGED. It surfaced only by reading the
  live box and finding a tool flag that should have been there and wasn't.

## Who writes what

Since the repo went public, more than one writer may be at work: several
sessions, or other contributors (Mike, 2026-10-31). Each feature goes on its
own branch and lands through its own PR, so CI and review gate it, and no
writer's commits ride into `main` on another's merge.

- **Task docs are not handoff specs.** The spec-heavy style — module names,
  function signatures, call sites, test expectations written out before any code
  — exists so a separate implementor can act without the reasoning in its
  head. `docs/tasks/schedules-and-reminders.md` is the worked example, and it is
  expensive to write. Reasoning-first is the default; write the concrete
  surfaces where *you* would otherwise re-derive them, not as a matter of course.

## CI — back on since 2026-10-28

Both workflows run in `marlow-assistant/marlow` (check with `gh api
/repos/marlow-assistant/marlow/actions/workflows` — `state` on each). They
were off from 2026-08-11 in the private repository, where the `ios` macOS job
ran the account out of Actions minutes. A public repository's Actions minutes
are free, macOS included, so that cost is gone.

**The mistake to avoid:** a pushed branch with no PR shows **no checks at
all**. `gh pr checks` says "no checks reported" and `statusCheckRollup` is
`[]`. That is not a pass, not a failure, and not a pending run — it is
*nothing*. Say "green in CI" only once a PR's `test` check has reported;
before that, say the local suite passed and give the count.
`.githooks/pre-push` (below) runs on every push, so it is the first gate.

- `.github/workflows/ci.yml` runs `pytest` on push to `main` and on **pull
  requests** — pushing a feature branch alone does *not* trigger it (confirmed
  2026-07-20: a bare branch push showed no run at all). Open the PR to get a
  real CI signal before merging; don't read a pushed-but-PR-less branch as
  "CI ran and passed." The check/job is named **`test`** (not `ci` — that's the
  workflow name). If you ever wire a required-check anywhere, the context
  string is **`test`**. Requiring `"ci"` would wedge the branch waiting on a
  check that never reports.
- **A second workflow gates `ios/`** (added 2026-07-31):
  `.github/workflows/ios.yml`, job **`ios-test`**, runs the Swift suite on a
  `macos-15` runner. It is a separate workflow rather than a second job in
  `ci.yml` because GitHub's path filters are **workflow-level** — that's what
  keeps a server-only PR on the fast Python gate alone. So there are now **two**
  check contexts, `test` and `ios-test`, and on any given PR one of them may
  legitimately not run at all. Don't read an absent `ios-test` as a failure, and
  don't wire a required check expecting both to always report.
- **Keep CI fast** (currently ~25s for the Python job; the macOS one is
  minutes, which is exactly why it's path-filtered). A fast gate is invisible; a slow one gets
  bypassed and resented. If the suite ever creeps toward minutes, split it: fast
  tests gate, slow ones run async.

## Server-side protection: two rulesets on `main` (2026-10-31)

Skipped while the repo was private, where rulesets need GitHub Pro. Public made
them free, and other writers became possible, so `main` now has two (repo →
Settings → Rules → Rulesets):
- **"main: no force push or deletion"** — no bypass for anyone, admins
  included. History on `main` is append-only.
- **"main: PR and passing test"** — a change reaches `main` through a PR whose
  `test` check passed (GitHub Actions' check, not the `ci` workflow name).
  `ios-test` is deliberately not required: it is path-filtered, so a
  server-only PR would wait forever on it. Repo admins bypass this one, which
  is how a docs-only change Mike OKs still goes straight to `main`.

The bypass belongs to the account, not the person: a session pushing with
Mike's credentials can bypass it too. What holds a session to the PR flow is
CLAUDE.md's ask-before-`main` rule, not GitHub.

## The local gate: `.githooks/pre-push`

Runs `pytest` before any push and **blocks a red push from leaving the
machine**, ahead of any CI run. It binds whoever pushes, Claude included.

- Enable once per clone: `git config core.hooksPath .githooks`
- Bypass a single push (mean it): `git push --no-verify`

### What it skips

Ambiguity always runs everything; these are the only skips:
- **Branch deletions only:** nothing pushed, nothing tested.
- **Docs only** (`docs/**`, top-level `*.md`; *not* `identity/*.md`): no suite.
- **Nothing under `web/`:** no Vitest/typecheck, and no `npm` needed.

An existing branch is diffed against what the remote has; a new branch against
its merge-base with `<remote>/main` as last fetched (a stale one only widens the
diff). No merge-base runs everything.

### Worktrees: the gate follows the repo, not the checkout

A linked worktree shares the repo but **not** `.venv`, so the hook used to find
no `pytest` and block the push. Blocking was the correct answer to a missing
runner — but the escape it made obvious was `--no-verify`, which with CI off is
an ungated push to the only gate this repo has, taken at the moment you are
least inclined to think about it.

**Fixed 2026-08-27**: the hook resolves the venv via `git rev-parse
--git-common-dir`, so a worktree borrows the primary clone's runner
automatically. Running that binary from the worktree still collects *the
worktree's* tests — pytest takes its rootdir from the cwd, not from where the
interpreter lives (verified the same day: 818 from the iOS worktree, which
predated a later batch of tests, against 839 from the primary).

Nothing to arm per worktree. A local `.venv` still wins if one exists, and a
clone with neither is still blocked rather than waved through.

## The flow for a non-trivial change

1. Branch off `main`. Write the change **test-first** — the existing suite is the
   model (real code exercised against a fake `claude` binary, not mocks).
2. Push the branch. The pre-push hook runs the suite and blocks a red push —
   the first gate. CI reports only once the PR is open (see "CI — back on"
   above): `test`, plus `ios-test` when `ios/` changed.
3. **Automated review on the branch** — this replaces the tech-lead review Mike no
   longer performs:
   - `/code-review high` on every non-trivial change.
   - **+ `/security-review`** when the change touches the **permission
     round-trip, egress, credentials, or auth**.
   - Findings go to Mike as a **short list**, not a diff to rubber-stamp. Address
     the real ones; Mike looks deeper only when his gut flags something or the
     change is load-bearing.
   - **Claude runs these itself.** If one refuses to launch, report that
     rather than handing the review to Mike.
4. `/code-review ultra` (the deep multi-agent cloud pass) is **Mike-triggered**
   and billed — Claude can't launch it. Reach for it on genuinely gnarly changes.
5. With findings resolved, ask Mike before opening the PR, and again before
   merging. Squash-merge so each change is one atomic,
   revertable unit on `main` (clean `git bisect` + `git revert` later).
   - **Note 2026-08-01:** a run of early PRs was landed as *merge commits*, drifting
     from this rule without anyone deciding to. **The next one was the first squash** and
     the rule stands as written — squash is the default, no per-PR debate.
     That first squash is why it matters: the branch contained a commit that introduced a
     CI bug and a later one that fixed it, so squashing put the correct end
     state on `main` as one revertable unit instead of leaving a bisect trap.
     If merge commits were ever the real intent, change *this line* rather than
     re-deciding at each merge.
6. **Landing work on a `docs/tasks/*` item updates that item's own Status line
   in the same commit.** Not a follow-up, not "later" — the same commit, or it
   does not happen.

   This is not bookkeeping hygiene; it is the single most expensive documented
   failure in this repo. An audit on 2026-08-01 found **four** task docs whose
   Status line was wrong, every one of them claiming *less* had been built than
   actually was: `scheduled-work.md` and `loader-identity-injection.md` both
   said "not started" long after shipping, `cache-friendly-identity-injection.md`
   said "awaiting review + merge" after both, and `apns-push-delivery.md` said
   `push.py` "is a stub" when it is fully implemented with 15 tests. START's own
   P2 and P3 entries were wrong the same way.

   The failure mode is specific and costly: these docs are what a fresh session
   reads to decide what to build, and CLAUDE.md *mandates* reading START first.
   A stale "not started" doesn't cause confusion — it causes a session to
   confidently **re-implement working, tested code**. Status drifts stale in
   exactly one direction, because the moment of completion is when nobody feels
   like editing docs.

   When a task is done, say what is *actually* left rather than deleting the
   entry — usually something real, and usually not code (`scheduled-work` needs
   a deploy-box install; `ios-minimal-client` needs a physical-device run). Keep
   the body as the spec it was built from; mark it as such so it is not misread
   as a to-do list.

7. **Changing what Marlow can DO means changing what Marlow is TOLD — in the
   same PR.** A new store it writes, a lifted restriction, a tool path, a moved
   file: if the affordance moved, `identity/CLAUDE.md` (the template here) and
   the live `~/marlow-identity/CLAUDE.md` on the box are part of the change,
   not a follow-up.

   The cost, measured on 2026-08-30: the declarative-schedules change shipped —
   store, validator, `file_policy` auto-allow, systemd timers, a clean deploy,
   and an idempotence check verified on the live box. Asked for a reminder that
   evening, Marlow reached for the Claude Code `schedule` skill and then
   `sleep 300`. Its own identity still said "cron changes are Mike's to apply"
   and "job timing is his to change, not yours", both written for the
   hand-edited crontab. It was obeying. The feature was flawless and did not
   exist.

   Two things make this failure mode worth its own step. **The suite cannot
   reach it** — every test passed, because the tests exercise the server and the
   gap was in a Markdown file in a different repo. And **omission is not
   neutral**: an identity that simply fails to mention a new capability doesn't
   produce hesitation, it produces confident routing to whatever mechanism the
   model *can* see. Silence picks the wrong tool.

   The check at merge time is one question: *if I asked Marlow to use this, what
   would it read?* If the answer isn't a line someone wrote on purpose, the
   change isn't done. Note the live file is a separate repo on the box and never
   shows up in `git diff` — it has to be looked at deliberately.

8. **Changing what the user would notice means telling them — in the same
   PR.** Step 7 is what Marlow is told; this is what the person is told. Add
   an entry to `changes.json` (policy and format in `docs/tasks/whats-new.md`)
   and both clients show it once, after the update lands. The bar is "would
   they be surprised to find out, or act differently if they knew?", and it is
   always met by a change to what Marlow can reach, what it does unattended,
   what it stores or sends off the box, or how it behaves. The check at merge
   time: *what will the card say?* If the answer is "nothing" for a change on
   that list, the change isn't done.

## Why this shape (the reasoning, for when you're not foggy)

- Most "PR-heavy" process exists to **coordinate humans** (required reviewers,
  CODEOWNERS, multiple approvals). Solo, all of that is dead weight — stripped.
- What's left underneath is worth keeping: **(a) a CI gate** so the trunk stays
  green, and **(b) atomic revertable history** so a break weeks later backs out
  cleanly. Neither needs Mike's eyeballs.
- For **AI-authored code**, the substitute for human review is *automated* review
  (tests + `/code-review` + `/security-review`), not putting a human back in the
  loop to perform diligence they aren't actually doing.

## Productionized version (future, deliberate step)

The interactive `/code-review` commands are the same engine as a **CI review bot**
that comments on every PR automatically — likely the shape the NemoClaw team uses.
Turning ours into a GitHub Action that posts review comments on PRs is a real
future step; it needs API-key wiring, so it's opt-in, not a freebie. Get familiar
with the interactive commands first (starting at gate #4), then productionize if
it's pulling weight.
