# Marlow

<img src="marlow-icon-square.png" alt="Marlow's portrait: a man in a dark Victorian coat with a glowing monocle, Big Ben behind him" width="200" align="right">

Marlow is a personal assistant that runs on a machine you control and talks to
you from your phone. It is built on Claude Code and runs under your own Claude
subscription (Pro or Max); there is no API key and no service in between.

What it does:

- **Chat, by text or voice,** from a phone or a browser, with notifications
  when it has something for you.
- **Remembers, in files you can read.** Its memory and personality are plain
  Markdown in a git repository on your box, so you can see what it knows,
  edit it, and see how it changed.
- **Runs scheduled jobs you configure:** a morning briefing from your email
  and calendar (through Claude's Gmail and Calendar connectors), reminders,
  any recurring task you describe to it.
- **Weather and route planning:** forecasts for where you are, and the
  weather at each stop along a drive.
- **Works with the web:** searches and reads pages for you.
- **Asks before acting.** Anything outside what it is allowed to do on its
  own comes to your phone as a question you approve or decline.

It has three parts:

- **A persistent server** (Python) on an always-on Linux box. Every request,
  from a client or a scheduled job, goes through it to a headless `claude -p`,
  one at a time.
- **An iOS app,** native SwiftUI.
- **A web app** for Android and desktop browsers, installable to the home
  screen, with push notifications.

Phones reach the box over [Tailscale](https://tailscale.com) by default. To
install one, see [`deploy/INSTALL.md`](deploy/INSTALL.md).

Marlow was written with Claude Code, and it uses the official `claude` CLI
unmodified, under each user's own subscription and its terms.

This is standalone, installable software. It ships **no personal identity** —
the assistant's `SOUL.md` / `CLAUDE.md` / `USER.md` / `memory/` are *data you
seed at deploy time* (`deploy/seed-identity.sh`), living in the runtime working
dir and diverging independently from there. The program and the personality are
deliberately separate things.

## Where this came from

Marlow was built in a private repository from July to October 2026: just over
1,000 commits in three and a half months, by Mike Mason working with Claude
Code. This repository starts from a snapshot of that work, so its history
begins with a single commit. The earlier history stays private because it
carries personal details that were removed for this release.

## What's here

```
server/     The application. invoke.py is the only module that shells out to
            `claude -p`; queue.py serializes invocations; app.py wires the
            public (Tailscale) + internal (loopback) ASGI apps. See
            docs/marlow-v2-implementation-plan.md §2 for the module map.
web/        The web client: TypeScript, Vite and React, served by the server
            itself from web/dist (built on the box, never committed). An
            installable PWA with Web Push. See docs/tasks/portable-web-client.md;
            the behaviour it reproduces is docs/chat-ux-contract.md.
tests/      pytest suite. Runs the real server code against a fake `claude`
            binary (tests/conftest.py) — plumbing is exercised, not mocked away.
deploy/     systemd unit, watchdog, off-host dead-man's switch, nftables egress
            allowlist, the update path, and seed-identity.sh. See deploy/README.md.
identity/   Empty template of the assistant brain a fresh install boots with.
            Placeholders only — real personality is seeded at deploy time.
docs/       Design record, gate results, and the build plan.
```

## Design constraints (decided — see docs/)

- CLI (`claude -p` / `--continue` / `--resume`), **never** the Agent SDK (the SDK forces API-key billing).
- One server-mediated work queue; only one `claude -p` runs at a time.
- **No public ingress by default** — Tailscale tailnet only, device-token auth. A box's owner can opt in to Tailscale Funnel (`update.sh --funnel`) so phones need no Tailscale, at the cost of a public endpoint; the trade-off is spelled out for users in `deploy/INSTALL.md` and built in `docs/tasks/funnel-ingress.md`. APNs and Web Push are wake signals; content is pulled from the box (Web Push payloads are also end-to-end encrypted, and a reply's text is never in one).
- Security boundaries (fs path-sets, egress host allowlist, env-injected secrets) declared as data in `server/boundaries.py`, enforced standalone now, shaped so a future sandbox policy is generated from them.

## Develop

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest        # the suite must be green before anything ships
git config core.hooksPath .githooks   # arm the pre-push hook (blocks red pushes)
```

The web client (Node 20.19 or later):

```sh
cd web && npm ci
npm test                # Vitest: the client's logic
npm run build           # writes web/dist, which the server serves at /
../scripts/dev-server.sh --reset   # in another shell: the server on
                                   # http://127.0.0.1:8080 with a fake `claude`
../scripts/dev-server.sh code      # a pairing link for the browser
npm run e2e             # Playwright against the dev server
npm run dev             # Vite with hot reload, proxying /v1 and /health to :8080
```

CI runs the pytest suite on pull requests and on `main`, and the iOS build when
`ios/` changes. Before that, the `.githooks/pre-push` hook runs the pytest
suite, plus `npm test` in `web/` when a push touches it, so a red suite never
leaves the machine.

## Deploy

See [`deploy/README.md`](deploy/README.md). In short: clone this repo as a
dedicated `marlow` user, seed an identity into the working dir once, install the
systemd unit. Updates are a manual `deploy/update.sh` (fast-forward-only pull,
reinstall, build the web client when it changed, restart, health-check) — the software checkout never diverges, so it
stays fast-forwardable; the seeded identity dir is separate and never touched by
updates. Pair a browser with a link from `deploy/pair-device.sh`, run on the
box.
