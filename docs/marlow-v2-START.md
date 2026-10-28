# Marlow v2 — session startup

You are working on `marlow-server` (`github.com/mikemason/marlow-server`), the
*software* behind Marlow. You are in **builder mode**, not acting as Marlow.
This repo does **not** carry Mike's assistant memory; that lives in his private
`claude-assistant` repo.

**This doc routes; it does not explain.** The truth about a piece of work is
its `docs/tasks/*.md` Status line. History behind shipped work goes in
the task doc; `marlow-v2-build-log.md` gets a routing row (and a trap only if no
task doc records it). If you are about to add a paragraph here, it belongs
in one of those instead. Hard constraints and working rules are in the root
`CLAUDE.md`.

## Read first

1. `docs/marlow-v2-implementation-plan.md` — **build from this**: server module
   layout and the iOS↔server protocol. Its §7 is the original plan, not a
   status board.
2. `docs/marlow-v2-gate1-results.md` — the confirmed `claude -p` CLI contract.
   Obey it.
3. `docs/marlow-v2-gate3-results.md` — what the server skeleton actually is.

**Only if relevant:**
- `marlow-v2-workflow.md`: branch, review and merge.
- `marlow-v2-identity-architecture.md`: `identity/`, `server/identity.py`,
  seeding.
- `marlow-v2-repo-structure.md`: `deploy/`.
- `marlow-v2-build-log.md`: history of shipped work.
- `marlow-v2-design.md`: rationale only, never build from it.

## Open work

Mike's wishlist lives on the box:
`ssh marlow@marlow 'cat ~/marlow-identity/memory/marlow-features.md'`. When
you finish something, mark it done there as well as here.

- **`friends-alpha.md`**: Marlow for 3–5 friends, each on their own
  subscription. The installer merged after a fresh Ubuntu dry run;
  first-run onboarding (its item 9) merged after three VM runs.
  The Android user's install went OK. Next, before inviting anyone else: Mike
  runs it hands-off on a fresh Multipass VM from `--ref main`.
  **Decided 2026-10-25: iPhone friends use the home-screen web app**
  (Safari push and the unread mark work). TestFlight and a push
  relay are parked. The ordered list of what comes before the next invite
  is at the top of `friends-alpha.md`. Repo filtering gates *every* new
  invite.
- **`box-upgrades.md`**: done. Both boxes are converted (the second box
  2026-10-14), so `update.sh` alone delivers identity changes.
- **`portable-web-client.md`**: shipped (2026-10-02), live on the box. Still
  open, on a real Android phone: push through Doze, the soft keyboard, and
  flinging during a stream.
- **`usage-visibility.md`**: shipped on both clients. Still open: the ≥90% yellow row (never seen on screen on
  either), and the Android user finding the panel. A fresh conversation
  starts at ~38K, and the morning cron jobs ran in the chat session, so idle
  compaction's 110K may still want re-checking against real curves.
- **`tool-approval-ux.md`**: the approval card and "Allow similar for this
  reply" shipped on both clients. Still open:
  device checks not yet seen, and pieces 2–3 (Drive).
- **`message-location.md`**: shipped on both clients (2026-10-10);
  observed on Mike's iPhone. Still open: Android Chrome's prompt and
  "blocked" footer.
- **`ideas-panel.md`** (2026-10-13): v1 shipped on both clients (store, panel,
  spotlight card, quoted Do it bubble; the lightbulb replaced the model chip).
  The nightly job ships in the template. Next: watch the Android user's first run, and
  the first spotlight on any box.
- **`whats-new.md`** (2026-10-23): the "Marlow was updated" card and
  Settings' What's new, from a repo-root `changes.json`; built on both
  clients, merged 2026-10-23; seen on Mike's iPhone, Android not yet. Next
  entry id is 7. Its policy (what gets
  an entry) is now a working rule in `CLAUDE.md`.
- **`ios-image-input.md`** (2026-10-23): camera shipped on both clients. Multi-image messages (up to 6, Mike's picks) shipped on both clients 2026-10-24, device-checked on the iPhone and the Android emulator's PWA. Follow-ups specced, not started: `image-viewer.md`, `message-retention-setting.md`, `share-and-files.md`.
- **`weather-mcp.md`**: merged 2026-10-13, live. Still open: read
  `weather.log` after a few days (two outages already, 10-13 and 10-14).
- **`bare-default-risk.md`** (2026-10-19): watching, nothing to build. The
  docs say `--bare` will become the default for `-p`. It skips OAuth,
  connectors, hooks and CLAUDE.md, and there's no opt-out yet. Read the CLI
  changelog before any `claude update` on a box or a new friend's install.
- **Durability lane** (good filler when blocked):
  - The `deploy/README.md` §7 watchdog is not installed.
  - Nothing alerts on a failed timer run (`failure-alert-channel.md`). The
    TLS renew timer works (renewed 2026-09-27, cert good to 2026-12-19) but
    nothing would say if it stopped.
- **`ios-chat-ux-gaps.md`**: the open iOS gaps. Its item numbers are cited
  from Swift comments, so never renumber them or reuse a number.
- **`model-shadow-run.md`**: ended 2026-10-17, code removed (the second
  user went Pro). The day-1 findings on Haiku stay in the doc.
- **`egress-and-webfetch.md`** (2026-10-16): reasoning, nothing built. Why
  egress isn't a web allowlist, the open WebFetch-in-cron path, and what
  OpenShell would and wouldn't add. Enforcement waits on Mike's NemoClaw
  re-check. The unattended WebFetch rule is future work, not a gate for
  the second instance (Mike, 2026-10-17).
- **`marlow-for-taylor.md`** (2026-10-17): live. A second Marlow on this box
  (the `marlow-server@` instance on :8444), on a second user's own Pro
  login: the second instance and the iOS paired host. Both
  instances run main; `update.sh` updates one at a time (`MARLOW_USER=` for
  the second). Still open: a push to that phone hasn't been observed.
- **`default-infrastructure.md`** (2026-10-21): the tidy-up, overnight ideas
  and check-ins become harness jobs, timed by "Your day" and "Check-ins" in
  Settings. Their output shows on Activity. Steps 1–3 shipped on both
  clients (2026-10-23), after the morning-after checks all passed; on
  both of Mike's box's instances. Step 4, the seeded `morning.md`, not started.
- **`onboarding-v2.md`** (2026-10-17): wording pass merged, harness
  not built. The second user's first run: the script lost its place after a
  time-zone detour, and it set things up before explaining them. Direction: shorter, structured,
  one step at a time; proposed harness: server-owned steps, a per-turn
  step line, server-checked completion (future). **Loose end:** the What's
  new card waits on onboarding by grepping USER.md for "Not yet known"
  (`changes.onboarding_complete`); replace it with that
  server-checked completion when it lands, and keep the template's
  placeholder, `FIRST-RUN.md`'s wrap-up and that string in step until then. Drive is parked
  upstream: probed on both accounts, the connector is Connected but gives
  `claude -p` no tools (Docs, Sheets and Slides do). Re-check after a CLI
  update with `scripts/probe-drive-connector.sh`.
- **Internet-accessible TLS endpoint** (`funnel-ingress.md`): **next**, before
  inviting more users (Mike, 2026-10-24); priority rose with each new user. Tailscale on
  phones costs battery and is one more install. Blockers 2 and 4 in
  `phone-access-without-vpn.md` come first and are right on the tailnet too.
- **`hosted-marlow.md`** (2026-10-17): far future. Install the app, connect
  Claude Pro, done, with Marlow on Mike's infra. Today's setup is
  enthusiast-only.
- **Terms** (2026-10-19): settled. Self-hosted use on each user's own
  subscription is the case the Claude Code legal page allows; the design
  principles that keep it so (no in-app Claude login relay among them) are in
  design §7. Also found: `--bare` becoming the `-p` default
  (`bare-default-risk.md`). License: Apache-2.0 (2026-10-24).
- **`marlow-faces.md`** (2026-10-16): shipped on both clients. Eleven faces,
  iOS icon follows; face and Thinking level sync through `/v1/preferences`.
  Still open: the web half on a real Android phone.
- **`voice-mode-android.md`** (2026-10-22): web dictation and web voice mode
  on box-side Parakeet, with a "Copy debug info" report, and `update.sh`
  installing the speech model by default. Merged (2026-10-23);
  passing on Mike's box from desktop Chrome and the Android emulator; with
  the Android user to try on the Pixel. The doc's top lists what only a real
  phone can check.
- **`ideas-seen-sync.md`** (2026-10-16): not started. Opening Ideas on one
  client should clear the dot on the others, via `/v1/preferences` like the
  face and Thinking level.
- **`text-size-setting.md`** (2026-10-15): not started. Web `px` to `rem`
  plus an in-app control; iOS already follows Dynamic Type. The Android
  user's ask.
- **`settings-overhaul.md`** (2026-10-21): not started. Both clients'
  Settings screens need a pass, plus an acknowledgements page for the
  Apache-2.0 code the app ships.
- **UX lane (`ux-lane.md`)**: done. Editing Marlow's files from the phone is
  a later problem (Mike, 2026-09-29).
- **Feature ideas:** `side-chats.md` is parked (Mike, 2026-10-12: not while
  the ideas panel is one tap away). `same-turn-store-validation.md` is parked
  (2026-10-19): a PostToolUse hook is proven to reach Marlow in the same turn,
  but nothing is built.
- **Deliberately not priorities:**
  - `nightly-consolidation.md` piece 3 (read `memory-file-shape.md` first);
  - `deferred-cron-review-findings.md` (don't self-assign);
  - `web-client-review-leftovers.md` (each item has its trigger);
  - `install-review-leftovers.md` (each item has its trigger);
  - `pytest-xdist` (`test-suite-speed.md` has the trigger).

Recently done, in case a summary elsewhere says otherwise:
- iOS hands-free voice mode with the Phonon voice;
- the settings path anchor, and `update.sh` refusing a rollback;
- box content scrubbed from docs and migration comments;
- the off-box bundle backup runs daily from launchd on the Mac, with a
  staleness notification (`bundle-backup-ssh-access.md`);
- iOS markdown tables, route planning;
- the weather tools, the ideas panel, message location, box migrations and
  the web halves of the Usage panel and approval card;
- `.githooks/pre-push` skips docs-only and non-`web/` pushes against the
  merge-base (`tests/test_pre_push_hook.py`);
- the turn source rename `ios` -> `chat`;
- the approval card and Allow similar, after the vetter rewidening;
- dictation runs iOS 26 SpeechTranscriber (`ios-modern-on-device-dictation.md`);
- the transparency sheet;
- one server-owned conversation;
- transcript windowing;
- token streaming;
- schedules;
- notification content swap.

Their task docs hold the detail.

## The two boxes

- **Dev:** this Mac, repo at `/Users/mike/nemoclaw/marlow-server`. Test with
  `.venv/bin/pytest -q` (Python 3.14) and push from here.
- **Deploy:** `marlow.<tailnet>.ts.net`. Always use that tailnet name,
  because the TLS cert is bound to it. Marlow runs under user `marlow`.
  - `ssh marlow@marlow` gets you in. **Check, don't trust a status line:**
    `git -C ~/marlow-server log -1`, `crontab -l`, and
    `~/.marlow/logs/`.
  - A `server/` change does nothing until Mike runs `update.sh`.
  - `sudo` there needs a real terminal.
  - This Mac has no working `tailscale` CLI, so set
    `MARLOW_PUBLIC_BIND_HOST` explicitly.
- **Builds** (2026-10-23): both instances ran `main` as of the what's-new
  merge (the second one plus a docs commit), the second with speech to text
  installed. Phones: Mike's had that change's branch build (same code as
  `main`), the second user's had `main` at the same point. Claude installs Debug builds with
  `xcodebuild` + `devicectl` (a phone on Wi-Fi only: build for
  `generic/platform=iOS`, install with `devicectl`).
- **CLI:** 2.1.301 on the box, for both `marlow` and `mike` (Mike, 2026-10-16,
  from 2.1.284). Re-checked the same day:
  - the init event lists exactly `VETTED_BUILTIN_TOOLS`, with Gmail 30,
    Calendar 9 and weather 3, and no settings warnings;
  - the hook-deny-over-allow contract and the `permission_denials` shape hold;
  - `probe-weather-mcp.sh` and `probe-shadow-fork.sh` (since removed) pass;
  - the `haiku` alias now runs Haiku 5.5.

  `mike` is **not logged in**, so `probe-allow-vs-hook.sh` ran as `marlow`
  through a `claude --setting-sources local` wrapper, which keeps the live
  hook out of the run. Probes run from `~/marlow-identity`
  (`probe-weather-mcp.sh`, ad-hoc checks) leave a session file among the real
  ones; delete it afterwards.

## Facts that bite

- **CI is OFF.** A PR shows no checks at all, which is neither a pass nor a
  failure. `.githooks/pre-push` is the only gate.
- **The iOS simulator lies about the transcript view.** Changes to
  `ChatView`'s scroll or layout need a device run (`xcrun devicectl`), and
  instrument rather than argue.
- **After changing `update.sh`, run it twice**, because it rewrites itself
  mid-run. It also syncs `deploy/marlow-settings.json` to the box and prints
  `policy CHANGED` when it moves.
- **No personal content in git, and no competitor app names** in committed
  text: history is forever, and this repo may go open-source. The way it
  leaks is real box output pasted in as evidence. See the rule in
  `CLAUDE.md`.
- **Identity injection is not carried by `--resume`.** Resupply it on every
  call. The planning number for a cache hit is a 240 s gap (`gate1-results.md`
  §8).
- **Session state stays out of memory** (`server/session_state.py` has the
  evidence). `server/idle_compaction.py` has the same trap, knowingly
  accepted.
- **Don't believe Marlow about its own harness.** Its self-diagnoses have
  been invented (`dated-plans-go-stale.md`). Check the connectors with
  `scripts/probe-connectors.sh` rather than taking its word.
- **Don't re-derive sizing by argument.** Re-run the measuring script
  instead (`tool-approval-ux.md`: `scripts/replay-bash-vetting.py`).
- **A turn can start without the claude.ai connectors,** on 1–3 turns most
  days. The tools usually come back within seconds, mid-turn, but eight
  `email-flash` runs since Oct 2 ran blind and replied `[silent]`. The sign
  is a `deferred_tools_delta` whose `removedNames` lists
  `mcp__claude_ai_Gmail__*`, or, caught live 2026-10-17, a stream `init`
  event with every claude.ai server `pending`. Cron turns now wait for MCP
  (`MCP_CONNECTION_NONBLOCKING=false`), and every turn logs
  `mcp_not_connected` to the activity log. The task, measuring whether that
  closes it, is `docs/tasks/connector-drops.md`.
- **A slow connector taxes every turn, even when it's denied.** Trello added
  about 30 s to CLI startup until Mike disconnected it (2026-10-21). He wants
  connectors like it back without that lag. The backlog task is
  `docs/tasks/connector-startup-latency.md`. The wider question is which
  connectors Marlow should see, what it may do with them, and sharing
  between Marlows through a shared service. That feature review is
  `docs/tasks/connector-policy.md`.
- **§9 egress is more urgent than it looks.** With `WebFetch` allowed, cron
  (which reads Mike's inbox) is where the exposure is real. Egress can't close
  that path; a hook rule on unattended WebFetch can
  (`docs/tasks/egress-and-webfetch.md`).

## Known gaps

The numbers are cited elsewhere, so don't renumber them.

1. **CLI-contract smoke test** (plan §9): re-assert auto-deny and the
   `permission_denials` shape on every deliberate CLI upgrade.
2. **`deploy/egress.nft` is deferred, and not live on the box** (checked
   2026-10-16). Plan and reasoning: `docs/tasks/egress-and-webfetch.md`.
   Bash in cron is already closed by the hook; the open path is WebFetch in
   cron, which egress can't close. Enforcement waits on Mike's NemoClaw
   re-check (from about 2026-10-30), since OpenShell would replace it. Before
   enforcing: hostname proxy, not resolved IPs (Cloudflare rotation), and the
   npm build off the `marlow` user (`deploy/README.md` §9).
3. ✅ Closed: harness-owned identity git commits.
4. **iOS perceived latency** is fixed at the transport/UI layer
   (`turn-activity-streaming.md`), never by a prompted "one moment".
5. ✅ Closed: the iOS permission round-trip UI.
6. **`session_hint` reaches `invoke.py` unvalidated.** This is defence in
   depth; both known injections are fixed. Read `invoke.py`'s `args` comments
   first, because the ordering is load-bearing. Hooks in inline `--settings`
   run even when the invocation then fails.
7. ✅ Closed: the PreToolUse hook is live (`deploy/marlow-settings.json`).

## Decided — do not re-open

- **Pairing:** an admin-issued single-use invite (a QR / link, 256 bits, ten
  minutes) via `POST /internal/admin/invite` on the owner-only socket;
  `deploy/pair-device.sh` prints it. Redeemed at `POST /v1/pair`.
- **Architecture:** standalone, own repo, seeded identity.
- **TLS:** the public listener requires it, and a bad cert is a startup
  failure, never a downgrade. Local dev opts out with
  `MARLOW_ALLOW_PLAINTEXT=1`.
- **Identity git is harness-owned.** `<working_dir>/.git` stays in
  `AGENT_CONFIG_PATHS` (RCE otherwise; see
  `tasks/marlow-working-dir-git-policy.md`).

## Still open — flag for Mike, don't assume

- **Retention:** the `_turns` numbers are undecided, and two queue gaps
  remain (`turn-retention.md`).
- **Auth:** the failure signature for an expired refresh token has not been
  captured yet; it finalizes `auth_monitor.py`.
- **Apple:**
  - Paid team, renews 2027-07-18.
  - APNs keys (production and sandbox), in
    `~/.marlow/apns/` on this Mac, **never** in the repo.
  - Topic: `com.mikemason.Marlow`.
- **`deploy/bootstrap.sh` is not started.** Mike asked for it: one
  idempotent script so a second host is repeatable.
