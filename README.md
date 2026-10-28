# Marlow

<img src="docs/images/marlow-portrait-400.jpg" alt="Marlow's portrait: a man in a dark Victorian coat with a glowing monocle, Big Ben behind him" width="200" align="right">

Marlow is a personal assistant that runs on a machine you control and can examine, and talks to
you from your phone. It is built on Claude Code and runs under your own Claude
subscription (Pro or Max).

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
- **An iOS app,** native SwiftUI, which you build and install yourself
  ([`ios/README.md`](ios/README.md)).
- **A web app** for Android, iPhone and desktop browsers, installable to the
  home screen, with push notifications.

To set up your own, follow [`deploy/INSTALL.md`](deploy/INSTALL.md). You need:

- an always-on machine running Ubuntu 24.04, with about 2 GB of RAM (an old
  laptop, a mini PC or a small cloud VM);
- a Claude Pro or Max subscription;
- a free [Tailscale](https://tailscale.com) account, which is how your phone
  reaches the box;
- about 15 minutes.

Marlow's use counts against your Claude plan, like your own use of Claude
Code. Light everyday use, a morning briefing and some chat, makes almost no
difference to a Pro plan.

Marlow was written with Claude Code, and it uses the official `claude` CLI
unmodified, under each user's own subscription and
[Anthropic's terms](https://www.anthropic.com/legal/consumer-terms).

Marlow starts out knowing nothing about you, with only a template of a
personality, and learns who you are by talking with you. Its character and
memory live in their own git repository on your box, separate from the
software, so updating Marlow never touches them.

## What leaves your box

There is no Marlow service in between. The box talks to:

- **Anthropic,** because each request is a Claude Code run: your messages and
  whatever Claude reads to answer them, as with your own use of Claude Code.
  Gmail and Calendar, if you connect them, are reached through your Claude
  account's connectors.
- **Weather and routing services** (Open-Meteo, MET Norway, Environment
  Canada, the US National Weather Service, OSRM), sent a location rounded to
  about 1 km when you ask about the weather or a drive.
- **Your phone's push service** (Apple's, Google's or Mozilla's), for a wake-up
  with no message text in it. The phone then fetches the reply from the box.
- **Tailscale,** which connects your phone to the box.
- **Web pages** Marlow reads for you.

## Where this came from

Marlow was built in a private repository from July to October 2026: just over
1,000 commits in three and a half months, by Mike Mason working with Claude
Code. This repository starts from a snapshot of that work, so its history
begins with a single commit.

## How it works

Each request, whether a message from your phone or a scheduled job, becomes
one headless `claude -p` run on the box, in Marlow's own working directory.
One queue on the server runs them one at a time, and a conversation carries on
by resuming its Claude session. When Claude wants to do something outside what
Marlow may do on its own, a hook holds the tool call and the server asks your
phone. Push notifications only wake the phone; the content is always fetched
from the box.

Some of the reasons behind that shape:

- **The CLI, not the Agent SDK.** The CLI runs on a Pro or Max subscription;
  the SDK needs an API key, billed per token. That's the point of Marlow: a
  personal assistant at a flat monthly cost.
- **One run at a time.** Runs share one working directory and one set of
  memory files, so the server never lets two of them edit those at once.
- **No public ingress by default.** The box answers only on your tailnet, and
  each device pairs with its own token. You can opt in to Tailscale Funnel
  (`update.sh --funnel`) so phones need no Tailscale, at the cost of a public
  endpoint; the trade-off is in [`deploy/INSTALL.md`](deploy/INSTALL.md).
- **Boundaries as data.** What Claude may read and write, and the hosts the box
  may reach, are declared in [`server/boundaries.py`](server/boundaries.py).
  The file rules are enforced by a sandbox around every run; an optional
  nftables allowlist enforces the host list ([`deploy/README.md`](deploy/README.md)).

The full design record is [`docs/marlow-v2-design.md`](docs/marlow-v2-design.md).

## What's here

```
server/       The application. invoke.py is the only module that shells out to
              `claude -p`; queue.py serializes runs; app.py wires the public
              (Tailscale) and internal (loopback) apps. Module map:
              docs/marlow-v2-implementation-plan.md §2.
hooks/        The PreToolUse hook that holds a tool call for your approval.
cron/         The entrypoint that turns a scheduled job into a queued request.
web/          The web client: TypeScript, Vite and React, served by the server
              itself from web/dist (built on the box, never committed). An
              installable PWA with Web Push.
ios/          The native iPhone app (SwiftUI) and its notification extension.
tests/        pytest suite. Runs the real server code against a fake `claude`
              binary (tests/conftest.py), so the plumbing is exercised, not
              mocked away.
deploy/       The installer, the update path, the systemd unit, watchdog,
              egress allowlist and certificate renewal.
identity/     The template a fresh install's personality and memory start from.
scripts/      Developer tools: the local dev server, probes of CLI behaviour,
              icon and data builders.
spikes/       One-off experiments kept for their findings.
docs/         Design record, build plan, and docs/tasks/, one note per feature
              or decision.
changes.json  The "What's new" list both apps show after an update.
CLAUDE.md     Instructions for Claude Code sessions working on this repository.
              Marlow's own instructions are identity/CLAUDE.md.
```

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

The iOS app: [`ios/README.md`](ios/README.md).

CI runs the pytest suite on pull requests and on `main`, and the iOS build when
`ios/` changes. Before that, the `.githooks/pre-push` hook runs the pytest
suite, plus `npm test` in `web/` when a push touches it, so a red suite never
leaves the machine.

## Deploy

To install, follow [`deploy/INSTALL.md`](deploy/INSTALL.md). Updating is one
command on the box, `sudo ~marlow/marlow-server/deploy/update.sh`.
[`deploy/README.md`](deploy/README.md) is the long-form reference for how a
box is put together and why, including the optional pieces the installer
leaves out.

## License

Apache-2.0: see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
