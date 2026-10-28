# Marlow for Taylor: a second Marlow on the box

**Status: building, on Pro (Mike, 2026-10-17).** Taylor gets her own Claude
Pro account, paid by Mike, and her instance runs in subscription mode like
his: Sonnet at Medium, the claude.ai connectors on her login. Still to do:
pieces 1, 5 and 6. Pieces 2–4 are gone: API-key mode was built and then
removed 2026-10-17 as dead code (Mike), along with the Haiku shadow run; both
survive only in the pre-snapshot history. The API key Mike made for her stays
in 1Password, unused: not on the box, not in any repo, never in a chat.

## What she wants

To be **organized**: Marlow as the source of discipline and reminders on
things she wants to keep on top of, rather than a chat companion (Taylor, via
Mike, 2026-10-16). That steers the build:

- The jobs that matter are **reminders and schedules** (`schedules-and-
  reminders.md`, already shipped), a **morning brief**, and **mail/calendar
  awareness**. Long free-form chat matters less.
- So the model question is mostly "can Haiku hold a plan and nudge reliably",
  which is the cron-side judgement the shadow trial measures. Chat quality is
  secondary.
- **The Google connectors are required, not optional** (below). An
  organizing assistant that can't see her calendar or mail is a reminder app.

## Decided (Mike, 2026-10-16)

- **Hosting:** her own instance on the existing Marlow box. It costs nothing
  and runs well for Mike. This revives the shelved option in
  `friends-alpha.md` ("Shelved: hosting friends on the Marlow box"); read its
  cost and blast-radius notes. The one that matters most: Mike has root and
  can read her mail and memory, so she should agree to that knowingly.
- **Client:** iOS, with builds installed over USB from Mike's laptop like his
  own phone's. TestFlight only if that gets tedious (piece 5).
- **Trust model:** settled. Taylor knows Mike runs the server.
- **Billing (superseded 2026-10-17, now Pro; see below):** pay-as-you-go on her API key. At Mike's own volume that is
  about $3–8/month on Haiku 5.5. Set a monthly spend limit on the key's
  Console workspace.

## API-key probe (2026-10-16, CLI 2.1.301, as `marlow`)

`scripts/probe-api-key.sh` (removed with API-key mode; in git history), with an empty `CLAUDE_CONFIG_DIR` and only
`ANTHROPIC_API_KEY` set after the invoke.py scrub. Q1–Q4 as hoped; Q5 is the catch:

- **Q1, runs headless:** `apiKeySource: ANTHROPIC_API_KEY`, `modelUsage`
  `claude-haiku-5-5`, `exit=0`, no prompt to approve the key. Piece 2 is an
  env var plus a config dir, nothing more.
- **Q2, no `rate_limit_event`:** her Usage panel can't show plan windows. It
  needs cost from the result's `total_cost_usd` ($0.0005–0.0009 per one-word
  turn here).
- **Q3, no connectors:** `mcp_servers: []`. The Google MCP is required, as
  expected.
- **Q4, `--strict-mcp-config` works:** with `--mcp-config` our stdio server
  connected and its tool listed. Her instance keeps the flag, and the Google
  MCP goes under it.
- **Q5, the agent's shell sees the key:** a Bash call printed `KEY_SET`. The
  key file can be bwrap-hidden, but the env reaches every tool child, so a
  `printenv` in a turn reads it. The value never appeared in the stream.
- **Q5b, the CLI-side scrub turns on the CLI's own sandbox:**
  `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` is recognised at 2.1.301. It forces
  permission mode to default (Marlow's mode already) and makes the CLI's Bash
  sandbox *required*: with explicit `--allowedTools=Bash`, every Bash call
  failed with "Sandbox is required but failed to initialize: ... socat not
  installed". So it is a hardening mode, not just an env filter. Using it
  means `socat` on the box, the CLI's sandbox nested inside our bwrap (never
  tested, and the AppArmor profile is in play), and every Bash call Marlow
  makes running under the CLI's own fs and network rules. Whether it then
  strips the key is still unmeasured.
- **Where that leaves the key:** in the env, readable by `printenv` in a turn
  (Q5). What bounds it is the Console spend cap and the egress allowlist,
  which limits where a read key could be sent. **It is not live:** `nft list
  table inet marlow_filter` on the box says no such table (2026-10-16). That
  is START.md known gap #2, deferred over Cloudflare IP rotation and the npm
  build. So today nothing stops an injected `printenv` plus `WebFetch` from
  sending the key out, and the same path already reaches Mike's own mail and
  memory. Bash `curl` and the like are already closed in cron (the hook
  auto-denies them); the open path is **WebFetch in unattended turns**, which
  egress filtering couldn't close anyway. That made the unattended WebFetch
  rule the go-live gate. **No longer (Mike, 2026-10-17):** with no API key on
  her box, the WebFetch rule is future work (`egress-and-webfetch.md`), not a
  gate; her instance has the same exposure as Mike's. Egress enforcement waits
  on Mike's NemoClaw re-check.

## The pieces to build

1. **A second instance.**
   - Its own Linux user, never in the `marlow` group, and its own working
     dir, data, logs, `~/.claude`, crontab and ports.
   - The server is already env-parameterized. The single-tenant assumptions
     are in `deploy/`: unit files hardcoding `/home/marlow`, the cert path
     and the AppArmor profile path.
   - Shape: a templated unit (`marlow-server@<user>.service`) or a second
     unit, plus a port per instance.
   - Rerun `scripts/probe-bwrap-confinement.sh` as the new user.
   - **Fixed 2026-10-16: the internal API trusted the whole box.** It
     listened on 127.0.0.1 TCP with no auth ("reaching 127.0.0.1 means you
     are on the box"). With a second tenant, a Bash call in either Marlow's
     turn could have minted a pairing code for the other instance, or queued
     a prompt there. It is now a Unix socket in an owner-only directory
     (`~/.marlow/run/internal.sock`, `MARLOW_INTERNAL_SOCKET`), and the
     directory is read-only inside bwrap so a turn can't swap in its own
     listener and approve its own tool calls. Mechanism confirmed on the box
     2026-10-16 (`probe-internal-socket.sh --standalone` as marlow, 8/8: the
     hook connects inside bwrap; the dir is read-only and a mount point there;
     ~ is read-only; the control is writable; user nobody is refused). Full
     probe on the live server 2026-10-17 after the deploy: 9/9,
     8464 closed. As her user, 2026-10-17 after her install: 9/9.
   - **Also found:** the crontab didn't mirror the internal address, so
     her cron jobs would have posted to Mike's server; it mirrors
     `MARLOW_INTERNAL_SOCKET` now.
   - **Installed on the box 2026-10-17** (from the `taylor-api-key-mode` branch;
     run the probes as `sudo -iu taylor`, since plain `sudo -u` keeps the
     admin's HOME and cwd). Her login is good to 2026-11-16, and
     `probe-connectors.sh` as taylor reached her Gmail and Calendar.
     `probe-bwrap-confinement.sh` as taylor: namespace OK, canary
     INCONCLUSIVE, because her own policy denies `~/.marlow` before bwrap is
     reached (confirmed: `permission_denials` holds the `cat`; the script says to run it as an admin for that reason).
   - **Built 2026-10-16:** `install.sh --instance
     <user> --public-port <port>` and `MARLOW_USER=<user> update.sh`
     (deploy/README.md §12). Her own unit rendered from Mike's, her own
     policy, `/opt/marlow/instances/<user>` copies, cert renewal, watchdog
     line and migrations record. The two update independently (Mike,
     2026-10-16: he deploys branches to his, hers only ever gets `main`). Each instance's user holds a copy of the
     node's TLS key, so `~/.marlow/tls` is now a secret path, hidden from
     every turn (security review, 2026-10-16). To do on the box: the install itself,
     `probe-bwrap-confinement.sh` and `probe-internal-socket.sh` as her user,
     and add her uid to `egress.nft` whenever egress is turned on.
2. **API-key mode in `invoke.py` (built 2026-10-16, removed 2026-10-17).**
   Her instance is on Pro, so `_scrubbed_env` strips `ANTHROPIC_API_KEY` for
   her as for Mike. If a friend ever needs it, restore it from the
   pre-snapshot history: the key came only from a 0600 file in data_dir, and config
   refused either half (mode or path) alone, so nothing could switch Mike's
   instance to API billing silently.
3. **Model and effort (no longer arises).** She gets Mike's defaults, Sonnet
   at Medium. Had it been Haiku: full model ids only (the `haiku` alias ran
   Haiku 4.5 on CLI 2.1.284), and idle compaction under the 100K-token price
   tier.
4. **Google connectors for her Gmail and Calendar (required).**
   - With an API key there is no Claude login, so there are no claude.ai
     connectors to borrow. Mike's Marlow gets Gmail and Calendar only because
     Anthropic registered those connectors and his Claude login grants them.
   - **The work:** a small MCP server on the box, our own like
     `server/weather_mcp.py` or a vetted off-the-shelf one, that calls the
     Gmail and Calendar APIs. Read-only first, matching the scope Mike's
     Marlow has (`search_threads`, the calendar reads); writes stay behind the
     phone approval.
   - **The Google side is only permission.** Register an app (an "OAuth
     client") in a Google Cloud project: free, no servers. Enable the Gmail
     and Calendar APIs, configure the consent screen, and Taylor signs in once.
     The box keeps the refresh token as a secret.
   - **Weekly re-auth, accepted (Mike, 2026-10-16):** while the app's
     publishing status is "Testing", Google expires refresh tokens after 7
     days. Mike takes that as a feature: a regular point where Taylor stays in
     control of the grant. Two things it then requires. The expiry must be
     *loud*: a failed refresh tells her to reconnect, never a brief that quietly
     skips mail. And re-auth must work from her phone, so the OAuth redirect
     has to land on the box over the tailnet (HTTPS on the ts.net name), not a
     loopback on the box. Check Google's current rules on both when building.
   - **A side effect worth having:** without account connectors,
     `--strict-mcp-config` is usable for her instance (probe Q4). It was
     dropped for Mike only because it strips the claude.ai connectors, so her
     instance gets a tighter MCP boundary than Mike's.
5. **iOS, two Marlows on one box.**
   - **Getting the app onto her phone (decided 2026-10-16):** the same way as
     Mike's. A Debug build installed over USB (`xcodebuild` plus
     `devicectl`); no TestFlight for now. One-time setup: her iPhone is
     registered to the team (Xcode does it on first plug-in) and she turns on
     Developer Mode (Settings → Privacy & Security, then a restart). The
     development profile lasts about a year on the paid team. Each app update
     means plugging her phone in. Debug builds use the sandbox APNs
     environment, which is what the box already runs. **TestFlight** (still
     open in `friends-alpha.md` item 7) is the fallback if plugging in gets
     tedious: over-the-air updates, 90-day builds, production APNs, and adding
     her as an internal tester avoids beta review.
   - **Pairing works:** the pairing screen takes a full URL with a port
     (`PairingView`), so her phone can pair to her instance's port.
   - **The paired host (fixed on `ios-paired-host`, 2026-10-17, not
     merged):** the app used the host typed at pairing for the pairing call
     only. Every later request, APNs token registration included, plus the
     notification extension's content fetch and the Settings "Server" line,
     used Mike's hardcoded `:8443`. Her phone would have paired and then got
     no pushes at all. `PairedHost` now stores it in the App Group for both
     targets. With nothing stored it falls back to `:8443`, so Mike's phone
     needs no re-pair.
   - **Tailscale:** her phone has to reach the box over the tailnet, by
     sharing the node with her.
   - **Push:** her instance can use the same team `.p8` key, since the
     bundle ID, and so the topic, is the same. Copy it to
     `~taylor/.marlow/apns/` (owned by taylor, 0600) and put the
     `MARLOW_APNS_*` values in her `20-local.conf`, with `KEY_PATH` pointing
     at her copy. The confinement takes that directory from the path, so her
     agent can't see it.
6. **Identity seeding.** She gets the stock `identity/` template, which
   since the goal-led first-run rewrite (merged into this branch 2026-10-17)
   includes that first run. `FIRST-RUN.md` opens with "what do you
   want to stay on top of?", sets up reminders while she watches (recurring
   ones too), points her at the Schedule panel, and writes up to two
   standing ideas. The seed also has the Sunday `week-ahead` starter job.
   `install.sh --instance` seeds her working dir through the same
   `seed-identity.sh --template` as a normal install. Migration 0005 is a
   no-op on that fresh seed, since the job is already there, and her record
   at `/var/lib/marlow/instances/taylor/migrations.applied` stores it as
   applied. Decided (Mike, 2026-10-17): her `USER.md` is not pre-seeded with
   what we know about her, because onboarding has to earn it. `email-flash`
   stays on, and `week-ahead` stays at Sunday 18:00.

## Order

1. Piece 1 on the box: Mike's instance to this branch, the full socket
   probe, then `install.sh --instance` with her Pro login at its stop.
2. Piece 5 (iOS fixes), then 6.
3. A week of her using it. **Started 2026-10-17:** her iPhone runs a Debug
   build of the `ios-paired-host` branch (USB, registered to the team), APNs is
   in her `20-local.conf`, and pairing to `:8444` worked; first run is hers.
   Push to her phone is not yet observed. Neither the unattended WebFetch rule nor egress
   enforcement is a gate (Mike, 2026-10-17).

## Decided: a Pro subscription for her (Mike, 2026-10-17)

Mike's number one goal is Taylor using a useful Marlow. With him paying for
her own Pro account, her instance runs like his: Sonnet at Medium on cron
(proven on his box), and the claude.ai Gmail and Calendar connectors on her
login. **That deletes piece 4** (the Google OAuth app, our own MCP server and
the weekly Google re-auth), the largest piece not yet built. Pieces 1, 5 and
6 still apply. Piece 2 becomes the existing subscription mode, and piece 3
(Haiku) doesn't arise. The API path was removed the same day (Status).

What Pro costs instead:
- Her own Claude use shares Pro limits with her Marlow.
- She inherits the connector drops (`connector-drops.md`).
- `--strict-mcp-config` is off the table for her instance, as for Mike's.
- **The Claude login needs re-doing about monthly.** Mike's box re-logged on
  Aug 18, Sep 14 and Oct 12; each grant ran 28–30 days from the login, and
  using it didn't extend it. `auth_monitor` warns about two days ahead. Her
  login needs her credentials. Without code: Mike starts `claude` on the box,
  sends her the link, she signs in on her phone and sends back the code. An
  in-app re-login that relays the code through Marlow is ruled out: sign-in
  stays in Anthropic's own flow (design §7, principle 2).

**`claude setup-token` doesn't avoid the re-login (probed 2026-10-17, CLI
2.1.301, as `marlow`).** It issues a token valid for a year, and
`CLAUDE_CODE_OAUTH_TOKEN` authenticates `claude -p` fine. But the
init event lists **no MCP servers**, with a scratch HOME and with the real
one. The normal login, same command, lists all seven claude.ai connectors
`connected`. The token grants inference only, so a Marlow on it has no mail
or calendar.

## Open questions

- Shared or separate APNs key handling (piece 5).
