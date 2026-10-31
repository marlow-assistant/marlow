# Friends alpha: Marlow for 3–5 people who aren't Mike

## Start here: before the next invite (2026-10-25)

In order. Each line is one task doc, and its Status line is the truth.

1. ✅ **Item 5:** Mike's fresh install from the public repo on a Multipass
   VM worked (2026-10-31).
2. Invite the first one or two friends. iPhone friends use the home-screen
   web app, so they need nothing more.

**Status:** installer merged (2026-10-02), first-run onboarding merged (2026-10-02).
The Android user's install (2026-10-05) went OK by Mike's account, so it
half-counts as the hands-off test: nobody recorded how much help was needed.
Direction picked (2026-09-29):
**self-host first** — each friend installs on their own Ubuntu box. Mike is
asking the group whether there's interest; building doesn't wait on Nov 8.
Hosting friends on Mike's box is shelved (both Mike and, he expects, the
friends prefer owning the box) — its analysis is kept below in case someone
asks for it.
Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

## What this is (and isn't)

Getting 3–5 friends who already use Claude Code onto their own Marlow, each
running on **their own Claude subscription**. The alpha exists to answer one
question: does someone who isn't Mike find it useful? It is not a product
launch.

Not in scope: hosted multi-tenancy as a business, bring-your-own API key,
cheaper models, or any feature work. Those change the CLI-only constraint the
whole design rests on, and they live in the assistant's
`memory/marlow-for-others.md` as "later, maybe". Features aren't the gap here.
A cold install is.

Constraints from Mike:
- **No new monthly cost for anyone** by default. They already pay for Claude.
  A cheap VPS is the one exception, and the friend's own choice.
- **The testers mostly have Windows PCs, not Macs.** Whether any of them has a
  Linux box is unknown.

## Where can a friend's Marlow run?

The server is Linux-only in practice, and that is structural. Confinement is
bubblewrap plus an AppArmor profile. Egress rules are nftables. Scheduling is
crontab. The service is systemd. The hook is a POSIX subprocess. So the options
come down to which Linux:

Best first (Mike, 2026-10-25):

| Target | Monthly cost | Always on? | Setup burden on friend | Verdict |
|---|---|---|---|---|
| **Old laptop / mini PC / Raspberry Pi** | $0 (one-off hardware) | Yes | Medium: install Ubuntu, then run our installer | **Best, for anyone who has one.** It's how Mike runs his own. A Pi 5 isn't needed: an idle Marlow is one Python process, plus the Node CLI while a turn runs, so a Pi 4 with 4 GB should do [unverified: nobody has run it on a Pi]. 64-bit Ubuntu either way. |
| **Cheap paid VPS** | ~£3 | Yes | Medium: sign up, SSH, Tailscale, then the installer | **Good.** The Android user runs on a £3/month VPS and it works well. The one option with a monthly cost. |
| **Another account on the Marlow box** | $0 | Yes, as reliably as Mike's own | **Near zero**: add the web app to the home screen, sign in to Claude once, pair | Shelved; see below. |
| **A box Mike sets up and hands over** (used thin client or Pi) | $0 (one-off hardware) | Yes | Near zero: plug in, log in to Claude, change the password | **Fallback** for a friend with no Linux box: install.sh run by Mike, then Mike steps out. |
| **WSL2 on their PC** | $0 | **No, by default.** The PC sleeps, WSL has an idle shutdown, and nothing starts it at boot unless a scheduled task does | Medium | **Unsupported for the alpha.** Scheduled jobs fire into a sleeping PC. bwrap/AppArmor under the WSL kernel is also unverified. Build support only if a tester actually asks. |
| **Windows, native** | $0 | if PC is | — | **No.** A port means replacing the sandbox, cron and the service manager. That is weeks of work, and the result ships without confinement. |

Not listed: **free-tier VPSes** (Oracle Always Free, GCP e2-micro). Oracle
reportedly reclaims idle free instances, and Marlow is idle most of the day; the
e2-micro's 1 GB is tight for the Node CLI.

## What to build, in order

Done (the build list, one line each): `deploy/install.sh` (idempotent, root,
Ubuntu 24.04; stops at the manual steps (`tailscale up` and the `claude` login),
and delegates shared steps to `update.sh` so an install and an update cannot
disagree; `deploy/INSTALL.md` is the friend-facing doc); Mike out of the
runtime (the per-turn context says "the user", never a name; the line in
Mike's `override.conf` is inert); a starter
identity in `identity/` with a morning briefing and nightly consolidation
(reasoning in `identity/AUTHORING.md`); nothing Mike-only on a fresh box.

Still to do:

5. **A dry run on a clean Ubuntu 24.04 VM** — first pass 2026-10-02
   (Multipass on Mike's Mac): install to a paired Chrome with push and a
   working approval round-trip. Fixed from it: `update.sh`'s branch check lost
   a SIGPIPE race under pipefail (34/100 on Ubuntu, 0/300 on macOS; also on
   `main`), the login stop now runs `claude` from an empty folder so its
   trust prompt is harmless, and `pairing-code.sh` no longer labels the code
   with a device name the browser seemed to have to match. The Android user
   installed (2026-10-05) and it went OK, though how hands-off it was went
   unrecorded.
   Done 2026-10-31: Mike's fresh install from the public repo on the VM
   worked, the gate before adding more friends.
6. **Repo access.** Done: the repo went public (2026-10-28), so a box clones
   it over HTTPS with no token or deploy key.
7. **TestFlight.** Parked (2026-10-25). iPhone friends use the web app
   added to the home screen from Safari, with notifications and the unread
   mark (confirmed on Mike's phone). It is less polished than the
   native app: Safari's voices are the basic ones. Friends who type their
   own host on the native app's pairing screen keep it (`PairedHost`, in the
   App Group); with nothing saved it falls back to Mike's box, which a
   distributed build must not do. A friend with a Mac can build the native
   app themselves.
8. **Push relay.** Parked with item 7: the web app's pushes go through
   Apple's web push service from each box directly, so nothing needs a
   relay Mike runs.

9. **First-run onboarding** (Mike, from the dry run): ✅ merged 2026-10-02.
   In the dry run nothing asked who the
   user was; Mike gave his name unprompted, and Marlow edited USER.md behind
   an approval. The shape:
   - Pairing the only device on a box where the user has never typed a
     message posts a canned greeting (`server/app.py` `FIRST_RUN_GREETING`)
     asking what to call them.
   - The starter `CLAUDE.md` has a three-line "First run" pointer, keyed on
     USER.md still saying "Not yet known", to a seeded `FIRST-RUN.md`: a
     guided first conversation that shows rather than tells. It uses their
     calendar and inbox list view (both vetted, no prompt), asks what they want
     help with, does one useful thing, offers a real reminder, checks the
     briefing time, and ends with one USER.md write.
   - First VM run (2026-10-02): the greeting landed. But the first script
     asked two things at once, got the name only, never came back to the
     rest, and wrote USER.md straight away. Hence one question in the
     greeting, a script that leads every step, and USER.md only at the end.
     Chat effort defaults to medium on server, web and iOS from the same run:
     a new user who finds it dim on day one doesn't come back.
     `scripts/reset-onboarding.sh` resets a test box to rerun it.
   - The name lives only in USER.md. The starter and the per-turn context block
     say "the user", and install.sh no longer writes the `MARLOW_USER_NAME`
     drop-in. That closes `install-review-leftovers.md` item 1.
   `identity/AUTHORING.md` "First run" has the reasoning.
   - **Goal-led revision (2026-10-17), not yet run.** The script now centres on "what do you want to stay on
     top of?", asked with real examples from a quiet seven-day calendar and
     three-day inbox read, and sets each answer up as a one-off reminder, a
     recurring `cron` + `notify` reminder (now documented in CLAUDE.md; the
     store always took it) or a memory note, then points at the Schedule
     panel. A Sunday 18:00 `week-ahead` job joins the seed as a starter
     prompt (Mike: on by default, `email-flash` stays on, no pre-seeding
     USER.md). Migration `0005-week-ahead` adds it to existing boxes, except
     one that already runs something on Sunday (Mike's `weekly-digest`), so
     on today's boxes it reaches the Android user's (unless they have a Sunday job) and
     Taylor's comes from the seed. Unverified: the whole
     conversation. To try it on a test box after `update.sh --branch`:
     `reset-onboarding.sh <hostname> --from-checkout`, pair, and check it
     asks the question with real examples, sets reminders without asking
     the phone, and mentions the Schedule panel. The reset puts schedules
     back to the box's own seed and a migration runs once per box, so a box
     seeded before this has no `week-ahead` after a reset; step 5 then just
     leaves it out. Taylor's install, seeded from this, is the real test.

   `update.sh` installs `CLAUDE.md` and `FIRST-RUN.md` itself on a box converted
   to the split identity (`box-upgrades.md`). Seeded identity is never
   overwritten, so a box that is not converted gets neither.

Parked: `renew-cert.service` runs `renew-cert.sh` as root from marlow's own
checkout. Fine while the agent sees the checkout read-only, but the watchdog
in install.sh already uses a root-owned copy for this reason; the cert
renewal should follow.

## Pairing papercuts (Taylor's install, 2026-10-17)

Feedback for later, not built (Mike). Pairing itself worked; the first-run
greeting replaced the big face straight away, as designed. Taylor's feedback on the
conversation itself is in `onboarding-v2.md`.

- **The pairing QR opens in the browser, not the app.** `pairing-code.sh`
  prints a QR of the web client's URL, so on her iPhone it opened Chrome.
  Mike's idea: the app registers for those URLs. Universal links need an
  Associated Domains entitlement naming the host, and every friend's tailnet
  name differs, so the entitlement can't list them; a custom scheme
  (`marlow://pair?host=…&code=…`) works for any box, but the QR then doesn't
  open the web client on Android. Decide which client the QR is for, or show
  both, before building.
- **The device name defaults to "iPhone".** `PairingView` uses
  `UIDevice.current.name`, which since iOS 16 returns only the model unless
  Apple grants the `user-assigned-device-name` entitlement on request. Without
  it, a better default is something we know: the user's name from the server
  once onboarding has it ("Taylor's iPhone"), or a rename in Settings. Check
  what the web client sends for its name in the same pass.
- **The first-run greeting wipes the big face** (Mike: "it just flashed up
  and went away, it didn't look intentional"). The empty-chat hero (120pt
  face, "Hi, I'm Marlow.") shows only while the transcript is empty, and the
  server posts `FIRST_RUN_GREETING` the moment pairing completes, so the
  hero lasts a blink and the greeting says "Hi, I'm Marlow" a second time.
  Proposed: keep the hero, as the top of the transcript, until the user's
  first message; Marlow's messages flow under it as bubbles, and it collapses
  to the header avatar, animated, when they send. Keyed on "no user message
  yet" rather than on the greeting text, so a cron message landing before
  they reply doesn't break it. iOS and web both.
- **Tailscale battery settings aren't in onboarding** (Mike, 2026-10-17).
  Left at its defaults the iOS tunnel is always on and costs battery. Mike set
  Taylor's phone from memory. The settings, read off Mike's iPhone
  (2026-10-17), on the Tailscale app's **VPN On Demand** screen:
  - **VPN On Demand:** on.
  - **Connect automatically on → Wi-Fi:** Do Nothing.
  - **Connect automatically on → Cellular:** Do Nothing.
  - **MagicDNS → Detect MagicDNS hostnames:** on. This is what brings the
    tunnel up when the app reaches `*.ts.net`, and it is only offered while a
    network is on "Do Nothing". Tailscale's own caption warns it "will not
    work for all apps nor all types of connections and is not immediate".

  Where they belong: the install's closing "put the device on your
  tailnet" text and a phone-setup section in `deploy/README.md`. Android has
  no On Demand; its answer is split tunneling (`phone-access-without-vpn.md`).
- **The pairing screen pre-fills Mike's server** (`PairedHost.fallback`,
  `:8443`), so a second instance's user has to edit the port by hand. The QR
  above, carrying the host, would remove that step too.

## Shelved: hosting friends on the Marlow box

Each friend gets their own Unix user and their own `marlow-server` instance on
Mike's box. Their Claude login, memory and connectors belong to their
subscription. Mike's quota carries none of it.

**Why this is cheaper than it looks.** The server is already fully
env-parameterized; the single-tenant assumptions live in `deploy/` (unit files
hardcoding `/home/marlow`, the cert path, the AppArmor profile path), so
several instances is mostly a templated unit (`marlow-server@<user>.service`)
plus a port per instance. Install friction is where alphas die, and every
tester would be on one version Mike controls, on a box he can already read
(`journalctl`). Box capacity, checked 2026-09-29: 7.7 GB RAM with 5.6 GB free
alongside v1 and v2, 4 cores, load under 0.2. The per-tenant cost is an idle
Python process plus a Node CLI only while a turn runs. Five friends fit, since
their turns rarely overlap.

**Costs, stated plainly. These go in the invitation, not a footnote:**
- **Mike has root, so Mike can read everything.** That includes their Claude
  OAuth token (`~/.claude/.credentials.json`), and through that token their
  claude.ai connectors: Gmail and Calendar. It also includes their memory and
  transcripts. No technical control fixes this. It's a trust choice, and some
  friends will reasonably say no. For them, self-host is the answer (next
  section).
- **Mike runs infrastructure for friends.** It's a basement laptop. When it's
  down for Mike, it's down for everyone, including while he travels.
- **Blast radius.** A confinement escape in any tenant's agent lands on the
  box that holds Mike's own secrets. Unix user separation is the wall:
  `/home/marlow` is `750`, so friends must never be in the `marlow` group. It
  is a real wall, but the confinement was designed and probed single-tenant.
  Rerun the bwrap probe (`scripts/probe-bwrap-confinement.sh`) as a tenant
  user before inviting anyone.

**Graduation path.** A friend who wants off Mike's box runs the same installer
on their own Linux, copies their working dir across (it's a git repo), and
re-pairs. That's why the installer has to be good regardless of where it runs.

## Open decisions (Mike's)

- ✅ Decided 2026-10-25: iPhone push comes from the web app, so TestFlight
  and the relay (items 7, 8) are parked.
- Route planning calls OSRM's public demo server (`router.project-osrm.org`,
  `docs/tasks/route-planning.md`). Its policy is light use: at most one request
  a second, an identifying User-Agent, no heavy use. The tool keeps to one a
  second itself. A few friends planning
  trips is that. Many more boxes means a self-hosted OSRM or a paid router.
