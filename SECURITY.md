# Security policy

Marlow runs an AI assistant on a box you own, with access to your files, your
accounts and, if you connect them, your mail and calendar. Its boundaries are
meant to hold: the permission hook, `server/boundaries.py`, the config-file
denies and the device-token pairing. If you find a way past one, we want to
know.

## Reporting a vulnerability

Please report it privately, not in a public issue:
[report a vulnerability](https://github.com/marlow-assistant/marlow/security/advisories/new).

Include what you found, how to reproduce it, and what it lets an attacker do.
A report against a stock install on the current `main` is the most useful.

Marlow is a personal project and replies are best-effort, with no guaranteed
response time. A report is read, and a confirmed one is fixed in the open once
a fix is out, with credit if you want it.

## In scope

- Escaping the filesystem, egress or environment-secret boundaries.
- Getting a tool call past the permission round-trip without the user's approval.
- Reaching the server without a paired device token.
- Writing to the agent's own config files (`boundaries.AGENT_CONFIG_PATHS`).
- Leaking secrets, tokens or the assistant's memory off the box.

## Out of scope

- Anything that needs an attacker who already has a shell on the box.
- Your Claude account or Anthropic's service: report those to Anthropic.
- Problems in a fork or a modified install.
