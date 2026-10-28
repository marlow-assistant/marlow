# Security policy

Marlow runs an AI assistant on a box you own, with access to your files, your
accounts and, if you connect them, your mail and calendar. This page explains
how Marlow limits what the assistant can do, where those limits stop, and how
to report a hole in them.

## Your responsibility

Marlow is an AI tool, and like any AI tool it can be wrong, be manipulated by
text it reads, and do something you didn't intend. The safeguards below reduce
that risk. They don't remove it.

- **You are responsible for watching what Marlow does.** Read the approval
  cards before you accept them, check what scheduled jobs are doing, and
  review its activity from time to time.
- **We can't guarantee that private information in Marlow, or in any tool you
  connect to it, will never leak.** That includes its memory, your messages,
  your files, and anything Marlow can reach through a connected service such
  as mail or a calendar.
- Only connect what you're comfortable with Marlow being able to read, and run
  it on a box that holds nothing you can't afford to expose.

Marlow is provided under the Apache-2.0 license, without warranty.

## How Marlow is designed to limit damage

Marlow doesn't try to make the assistant trustworthy. It assumes the assistant
can be tricked, for example by a web page or an email that contains
instructions, and puts its limits where the assistant's own judgment doesn't
matter. They work in layers, so one failing doesn't open everything.

**Every action passes a checkpoint.** Each tool call the assistant makes goes
through a hook that runs before the action does. The hook first refuses
outright anything on a never-allowed list. For the rest, shell commands are
checked against a short allowlist of well-understood commands, and each part of
a chained command is checked on its own, so an allowed command can't carry a
disallowed one with it. Anything not clearly safe is sent to your paired phone
as an approval request. If you don't answer, or the server can't be reached,
the answer is no. For scheduled jobs that run while you're away, anything that
would need your approval is denied.

**Secrets are out of reach.** Claude's login credentials, SSH keys, the
server's device tokens and TLS key, push-notification keys and `.env` files
are denied by the hook and also hidden from the assistant's processes by a
mount namespace (`bwrap`), so a command that expands in a way the hook didn't
anticipate still can't read them.

**The assistant can't rewrite its own rules.** Claude Code reads its settings,
MCP server configuration, plugins and hooks before the first tool call, so
before the hook exists to object. A writable config file would therefore be
arbitrary code execution outside every other safeguard. Those files, and the
assistant's own `.git` metadata, are on the never-write list, and a test keeps
them there.

**The assistant can't approve its own requests.** Pairing a new device is an
admin action on a separate socket in a secret directory, so the assistant
can't mint a device token and then approve its own tool calls.

**The server isn't on the internet by default.** It listens on your tailnet
only (Tailscale), and each phone or browser pairs once and holds its own token,
which you can revoke. Public access through Tailscale Funnel is off unless you
turn it on. If you do, pairing is invite-only, request sizes and times are
capped, even `/health` needs a token, and a test pins exactly which endpoints
the public side serves.

**Boundaries are code, with tests.** The filesystem rules, the permission hook
and the ingress surface live in `server/boundaries.py`, `hooks/` and
`server/bash_policy.py`, and the test suite exercises them against a fake
`claude` binary. Changes to these areas get a security review.

## What these safeguards don't cover

We'd rather you know than assume.

- **Outbound network traffic isn't filtered by default.** The repo includes an
  egress allowlist (`deploy/egress.nft`), but it's off, and it isn't tested
  enough to recommend. Any web read can carry data out in the URL, so a
  successful trick could leak what the assistant can see. The approval
  prompts are the main defense here, which is why reading them matters.
- **Prompt injection isn't solved.** Text in a web page, email or document can
  try to steer the assistant. The checkpoint limits what a steered assistant
  can do. It can't tell whether the assistant is being steered.
- **What's inside the allowed area, Marlow can read.** That's its working
  directory, its memory and whatever you've connected. The boundaries keep it
  out of everything else.
- **Connected services follow their own rules.** Mail and calendar reach Marlow
  through your Claude account's connectors. What they expose, and what
  Anthropic and those services keep, is governed by their terms and your
  settings, not by Marlow.
- **Your messages go to Anthropic**, since every request is a Claude Code run
  under your own subscription.
- **Approvals are only as good as your attention.** A request you approve
  without reading is an approval.
- **Funnel is a public endpoint.** If you turn it on, anyone can open a
  connection to your box, and the hardening above limits what they can do with
  it, not that they can try.

## Reporting a vulnerability

Please report it privately, not in a public issue:
[report a vulnerability](https://github.com/marlow-assistant/marlow/security/advisories/new).

Include what you found, how to reproduce it, and what it lets an attacker do.
A report against a stock install on the current `main` is the most useful.

Marlow is a personal project and replies are best-effort, with no guaranteed
response time. A report is read, and a confirmed one is fixed in the open once
a fix is out, with credit if you want it.

### In scope

- Escaping the filesystem or secret-path boundaries.
- Getting a tool call past the permission round-trip without the user's approval.
- Reaching the server without a paired device token.
- Writing to the agent's own config files (`boundaries.AGENT_CONFIG_PATHS`).
- Leaking secrets, tokens or the assistant's memory off the box.

### Out of scope

- Anything that needs an attacker who already has a shell on the box.
- Your Claude account or Anthropic's service: report those to Anthropic.
- Problems in a fork or a modified install.
- The gaps listed under "What these safeguards don't cover", which are known.
  A working exploit of one is still welcome.
