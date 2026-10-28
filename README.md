# marlow-server

An always-on Python server that is the single entry point for every Claude Code
invocation on a private host — from a native SwiftUI iOS/macOS client, a web
client (an installable PWA, for Android and desktop browsers) and from cron. It shells out to headless `claude -p` (keeping a Claude Pro subscription,
**not** API billing), serializes all invocations through one work queue, and
routes the rare tool-permission question to a paired phone via a bounded
round-trip.

This is standalone, installable software. It ships **no personal identity** —
the assistant's `SOUL.md` / `CLAUDE.md` / `USER.md` / `memory/` are *data you
seed at deploy time* (`deploy/seed-identity.sh`), living in the runtime working
dir and diverging independently from there. The program and the personality are
deliberately separate things.

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

CI is off (see `CLAUDE.md`). The `.githooks/pre-push` hook is the only gate:
it runs the pytest suite, plus `npm test` in `web/` when a push touches it, so
a red suite never leaves the machine.

## Deploy

See [`deploy/README.md`](deploy/README.md). In short: clone this repo as a
dedicated `marlow` user, seed an identity into the working dir once, install the
systemd unit. Updates are a manual `deploy/update.sh` (fast-forward-only pull,
reinstall, build the web client when it changed, restart, health-check) — the software checkout never diverges, so it
stays fast-forwardable; the seeded identity dir is separate and never touched by
updates. Pair a browser with a link from `deploy/pair-device.sh`, run on the
box.
