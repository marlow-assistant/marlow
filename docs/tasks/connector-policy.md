# Connector policy — use the claude.ai ecosystem on purpose

**Status: BACKLOG, feature review not started (Mike, 2026-10-21).** No
direction picked. This doc frames the decision. It's a user-facing feature,
so whatever ships needs an iOS half and a `web/` (Android) half. Related:
`connector-startup-latency.md` covers the cost of a connector merely being
attached. This doc covers which connectors Marlow should use, and how
much it can do with them.

## Why

The claude.ai connector ecosystem is a big lever that Marlow uses almost by
accident. Mike's Marlow gets Gmail and Calendar because his login grants
them. Everything else is either denied by a hand-kept list or falls through
to a phone prompt. Three things push for a deliberate design:

- **Other users are connecting things.** An Android user on a friends-alpha
  box is likely attaching connectors to their own account. Each one appears
  in their Marlow with no policy decided for it.
- **Sharing between Marlows.** Mike wants to share some things with his
  wife's Marlow, such as this winter's "No-demo Reno the House" project. A shared
  Trello board, reached by both Marlows through their own Trello connectors,
  would do it. The ecosystem carries the sharing, so the two Marlows never
  talk to each other directly.
- **The original worry is still valid.** Mike wants claude.ai to be able to
  use a connector without his always-on agent getting it too. One account
  can't express that today.

## How it works today

- **Connectors belong to the account, so Marlow gets all of them.** `claude
  -p` runs as the user's login (gate #1), so every claude.ai connector they
  attach lands in Marlow's surface. That holds per instance: each Marlow on a
  box gets its own user's connectors (`marlow-for-taylor.md`).
  `--strict-mcp-config` would scope this, but it strips Gmail and Calendar
  too, so it stays off the table (CLAUDE.md).
- **Policy is a static list in `deploy/marlow-settings.json`.**
  - Gmail and Calendar *read* tools are allowed tool by tool
    (`_connector_comment`).
  - Their write tools fall through to the phone prompt, and auto-deny in cron.
  - Trello, Excalidraw, Drive and Mermaid Chart are denied at server scope
    (`marlow-tool-surface-trimming.md` §4).
  - Any other connector falls through to a prompt. That fails closed, but it
    means an unknown connector buys friction on every call, not a capability.
- **Denying a connector hides it but doesn't disconnect it.** A deny entry
  removes the tool names from the listing. The connector still connects at
  startup, as Trello's 30 s lag showed (`connector-startup-latency.md`). So
  visibility and connection cost are separate problems.
- **The list is written for Mike.** It names his connectors. A friend's
  Trello isn't denied because nobody wrote it down, so the default for
  anything new is "shown, and prompts on use".

## The decision, split into its parts

1. **Visibility: does Marlow see the connector at all?** Today that means a
   deny or no deny. The proposal: **opt-in per connector, per user**. A newly
   attached connector stays hidden from Marlow until its user turns it on.
   That answers "claude.ai can use it, my agent can't" without a second
   account. It looks buildable: the stream's `init` event already lists every
   MCP server by name (`invoke.parse_init_mcp_status` reads it), so the
   harness can find connectors and pass `--disallowedTools` for the ones not
   enabled. `--disallowedTools` is the listing filter that's already proven.
   Open question: the first turn after a connector is attached would see it
   before the harness knows it exists. Either deny by pattern or accept one
   prompt-gated turn.
2. **Authority: once Marlow sees it, what can it do without asking?** The
   per-tool split that Gmail has (reads allowed, writes prompt) needs a rule
   that scales. A hand-enumerated list doesn't cover connectors nobody has
   seen. MCP tools can carry a `readOnlyHint` annotation. Check whether the
   CLI exposes it and whether connectors set it honestly. Otherwise the
   options are per-tool choices in the UI, or a coarse per-connector setting:
   *off / read / ask / allowed*. Cron is the hard case. A shared board Marlow
   updates unattended means cron writes, and that's exactly what the policy
   auto-denies now.
3. **Where the policy lives and who edits it.** It's per user, not per box,
   and the user edits it from the phone. So it goes in a server-side store
   rendered into each turn's flags, not in the shipped settings file. Both
   clients need a Connectors panel. The deny list in `marlow-settings.json`
   either becomes the default for Mike's box or goes away.
4. **Sharing between Marlows.** Recommend sharing through a third-party
   service (a shared Trello board, a shared calendar or Drive folder) over
   any direct link between instances on the box. It uses access control that
   already exists, works across boxes, and Mike and his wife can see and edit
   the same thing in the service's own app. A shared directory on the box
   would cross the per-user isolation boundary, so it's a bigger change.

## Risks to weigh in the review

- **Shared content is someone else's input.** A board another person writes
  to is untrusted text in Marlow's context, as an inbox is. That's
  low-stakes between spouses but not in general. It matters most in cron,
  next to Gmail, with WebFetch open (`egress-and-webfetch.md`).
- **Each connector you enable costs latency and context.** Opt-in limits that
  to what's enabled. Hidden connectors still cost startup time.
- **Identity update.** Whatever ships, `identity/CLAUDE.md` has to tell
  Marlow how connectors work. It needs to know that its tool list reflects
  the user's choices, and to say "turn on Trello for me in Settings" rather
  than claim it can't (CLAUDE.md working rules).
- **Connector drops apply to every connector** (`connector-drops.md`). A cron
  job that relies on a shared board has the same blind-first-look risk
  `email-flash` has.

## Next

- Mike: confirm the framing, especially opt-in as the default and sharing
  through a service rather than between instances.
- Inventory what an Android user's box actually lists at `init`
  (`scripts/probe-connectors.sh` as that user), to size the friction today.
- Check whether `readOnlyHint` (or any per-tool annotation) reaches the CLI's
  tool listing for claude.ai connectors.
- Then the design: the store's shape, the flag rendering in `invoke.py`, the
  panel for both clients, and the identity text.
