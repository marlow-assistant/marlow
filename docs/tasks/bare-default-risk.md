# `--bare` becoming the default for `claude -p`

**Status: WATCHING (2026-10-19).** Nothing to build until a CLI release makes
the switch. There is no opt-out today, so `server/invoke.py` passes nothing.
`tests/test_invoke.py::test_a_turn_never_runs_in_bare_mode` pins the part we
control. Before any `claude update` on a box, read the changelog (below).

## The finding

Found while reading the Claude Code docs on headless subscription use. An
engineering dependency, not a terms question. The headless docs
(code.claude.com/docs/en/headless, read 2026-10-19) say `--bare` is the
recommended mode for scripted calls and "will become the default for `-p` in a
future release". No date and no version.

Per the docs, bare mode:

- **never reads OAuth credentials or the keychain.** Auth must come from
  `ANTHROPIC_API_KEY` or an `apiKeyHelper` in `--settings`, so it can't run on
  the subscription. That billing is the reason this project uses the CLI.
- **skips auto-discovered MCP servers.** Only `--mcp-config` servers connect,
  so the claude.ai connectors (Gmail, Calendar) go and weather stays.
- **skips hooks, CLAUDE.md, skills, plugins and auto memory.** No PreToolUse
  hook means no permission round-trip and no secret-path denies.
  `~/marlow-identity/CLAUDE.md` (the agent's rules, which import
  `PREFERENCES.md`) is auto-loaded, not injected (`server/identity.py`), so it
  would go too. The docs list `--settings` as the way to load settings, which
  suggests `~/.claude/settings.json` and its deny rules go as well. Not verified.
- **drops system reminders and background tasks.**
- **stops installed mods.** The mods overview names `--bare` with
  `disableAllHooks` and `--safe-mode` as what stops them. A mod's hooks
  otherwise run in `-p` (only its drawing doesn't), so this matters if the
  PreToolUse round-trip ever moves into a mod. A mod loaded with
  `--plugin-dir` might survive, since bare mode loads that flag's plugins.
  Not verified.

The same CI gap hits Anthropic's own documented route. The authentication
docs recommend `claude setup-token` and `CLAUDE_CODE_OAUTH_TOKEN` for CI on a
subscription, and say bare mode doesn't read that variable. That's a reason to
expect an opt-out when the switch ships. It's an inference, not a promise.

## Is there an opt-out?

No, as of 2026-10-19. The CLI reference lists `--bare` and no `--no-bare`. The
env-var reference documents `CLAUDE_CODE_SIMPLE` only as "Set to `1`", meaning
"equivalent to passing `--bare`", and gives no off value. (Its neighbour
`CLAUDE_CODE_SIMPLE_SYSTEM_PROMPT=0` is unrelated: it shortens the prompt and
keeps hooks, MCP and CLAUDE.md.) `--safe-mode` is not an opt-out either. It
keeps auth but drops CLAUDE.md, hooks and MCP, so it's just as fatal.

`claude --help` was not run here: this session's own CLI is not the box's, and
nesting the CLI is off-limits. The box check below covers that.

## Why it fails closed, and the fix to refuse

When the switch ships, every turn fails. It does not quietly bill an API key:
`_scrubbed_env()` strips `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and
`ANTHROPIC_BASE_URL`, and bare mode won't read OAuth, so the CLI has no
credentials at all. The docs say a missing-auth failure "prints the failure as
the result on stdout". The scrub also strips any inherited
`CLAUDE_CODE_SIMPLE` (prefix rule). The new test checks that against the env
the child process actually receives.

**Never fix it by giving the CLI an API key or an `apiKeyHelper`.** That
gets turns running again, but on API billing, with no hook, no CLAUDE.md and
maybe no deny rules. The only fix is the release's own opt-out.

## Exposure

- **Existing boxes:** only after someone upgrades them on purpose.
  `deploy/marlow-server.service` sets `DISABLE_AUTOUPDATER=1`, and
  `deploy/update.sh` never touches the CLI. A `claude update` run by hand is
  the trigger.
- **Fresh installs:** `deploy/install.sh` runs the vendor installer, which
  installs the latest release. A friend installing after the switch gets a box
  that fails its first message. That's the real exposure (`friends-alpha.md`).

## Detection today

Loud but undiagnosed. A bare-mode turn exits non-zero and reaches the phone as
a failed turn. The journal shows `claude -p exited 1: ` with an empty stderr,
the same shape as the 2026-09-28 Cloudflare 522. `_looks_like_auth_failure`
reads only stderr, and the docs put this failure on stdout, so
`auth_failure_suspected` probably stays False. `mcp_not_connected` won't flag
it either: it lists servers whose status isn't `connected`, and a connector
bare mode never loaded isn't in the list at all.

**Not built, on purpose:** stdout auth-text sniffing or a "connectors missing"
check. Neither signature has been seen, and the felt symptom (every turn failing
right after an upgrade) is loud enough. If it lands, read the real envelope off
the box and teach `_looks_like_auth_failure` that string. Don't guess it now.

## What to watch

A weekly scheduled task on Mike's Mac (`claude-cli-update-check`, Mondays
09:00, read-only) does this. Mike wants to stay reasonably current and stop
before the switch, because a box far behind on the CLI is its own risk. The
task reads the box's `claude --version` over ssh and checks newer releases in
the changelog against the list below. It reports UPDATE OK, HOLD AT X, or
NOTHING NEW. Updating stays manual.

- The CLI changelog, github.com/anthropics/claude-code `CHANGELOG.md`, before
  any `claude update` on a box and before inviting a new friend. Look for
  "bare", "`-p` default" or `CLAUDE_CODE_SIMPLE`.
- The headless docs' "Start faster with bare mode" note. Today it reads "will
  become"; that changes when it ships.

## When it lands

1. The release will name the opt-out. If it's a **flag**, add it to the argv
   in `server/invoke.py` ahead of the `--` separator. Use the single-token
   form, like `--tools=` and `--effort=`. If it's an **env var**, it must go in
   `_FORCED_ENV`: the `CLAUDE_CODE_` prefix scrub would strip it from the
   systemd unit, and the setting would look like it worked while doing nothing.
2. Extend `test_a_turn_never_runs_in_bare_mode` to assert the opt-out is
   present.
3. Pin the installer to a pre-switch version until 1–2 ship, or ship them
   together.

## Box check after any CLI upgrade

Run on the box in a clean shell, as the instance user, from its working
directory. It is not run from a Claude session.

```bash
cd ~/marlow-identity && claude --version && claude auth status --text && claude -p --output-format stream-json --verbose 'reply with the word ok' </dev/null | jq -c 'select(.type=="system" and .subtype=="init") | {apiKeySource, mcp: [.mcp_servers[]? | "\(.name)=\(.status)"]}'
```

Healthy output: auth status shows the claude.ai login, the init line lists
the claude.ai Gmail and Calendar servers, and the run prints an init line at
all. If bare is the default, expect an auth failure and no claude.ai servers.
