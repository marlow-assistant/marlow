#!/usr/bin/env bash
# Probe: does `--strict-mcp-config` strip the Claude.ai connectors, and does the
# `marlow` account have them at all?
#
# WHY: v2 is meant to reach parity with v1's morning jobs, two of which need
# Gmail and Google Calendar. The assumption going in was "v2 needs a Gmail MCP
# server configured". Reading v1 on the box on 2026-08-06 showed that is wrong:
#
#   * ~/.claude.json has NO mcpServers, anywhere -- not top-level, not per-project.
#   * The only installed plugin is discord@claude-plugins-official.
#   * v1's settings.local.json allows `mcp__claude_ai_Google_Calendar__list_events`.
#
# The `claude_ai_` prefix is the tell: these are ACCOUNT-level connectors, not
# anything configured on the box. Which turns one assumption into two real
# questions, and they are independent:
#
#   Q1. Does the `marlow` account have connectors? Connectors are per-account
#       and `marlow` holds its own separate OAuth grant (deploy/README.md §3).
#       v1 runs as `mike`. NOTHING about mike's connector set transfers, so this
#       must run as `marlow` even though that is the more awkward user.
#   Q2. Does `--strict-mcp-config` remove them? server/invoke.py passes it on
#       every call, and it is a HARD CONSTRAINT in CLAUDE.md ("no implicit MCP
#       inheritance"). If it strips connectors, parity needs that constraint
#       relaxed -- Mike's decision, not a session's, and worth nothing until
#       this has been measured.
#
# WHAT COUNTS AS EVIDENCE. Not the model's self-description -- asked to list its
# tools, a model will happily produce a plausible list either way. The hard
# signal is a `tool_use` block in the stream naming a tool that starts with
# `mcp__`: the CLI only emits that for a tool that actually exists in the
# session. So this runs with --output-format stream-json and greps the events.
#
# A DENIAL IS A PASS. We are asking whether the tool is OFFERED, not whether it
# runs. The runs below deliberately grant nothing: in headless, a fall-through
# auto-denies (gate #1 §4), and the tool_use event is emitted before the denial.
# "Attempted and denied" answers the question completely.
#
# RUN THIS IN A CLEAN SHELL (plain ssh/terminal), NEVER inside a Claude Code
# session -- a nested `claude -p` inherits the parent's env and toolset and
# gives false results. See CLAUDE.md.
#
# RUN IT AS `marlow`:   sudo -u marlow -H bash scripts/probe-connectors.sh
#
# Unlike the bwrap probe (which ran as mike precisely to stay off Mike's phone),
# this one HAS to be marlow -- see Q1. It therefore writes its own settings file
# with NO hooks block, so nothing reaches the permission round-trip and no
# notification fires. It never touches marlow's real ~/.claude/settings.json.
#
# COST: 6 `claude -p` calls against the Pro subscription. Small, not free.
set -uo pipefail

# `|| exit` because a setup failure that lets the run continue produces output
# that reads exactly like a real negative -- see the 2026-08-06 note in
# probe-allow-vs-hook.sh. /tmp rather than $PWD is fine here: this probe never
# asks the CLI to write a file, so the sandbox's write confinement is not in
# play.
OUT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/marlow-probe-connectors.XXXXXX")" || exit 1
trap 'rm -rf "$OUT_DIR"' EXIT

# `sudo -u marlow -H bash` gets a minimal PATH that does not include
# ~/.local/bin, so a bare `claude` is not found -- which is how the first run of
# this probe (2026-08-06) produced six identical "No such file or directory"
# results. Prefer the same absolute path the systemd unit passes.
CLAUDE_BIN="${MARLOW_CLAUDE_BIN:-}"
if [ -z "$CLAUDE_BIN" ]; then
    for candidate in "$HOME/.local/bin/claude" "$(command -v claude 2>/dev/null || true)"; do
        [ -x "$candidate" ] && CLAUDE_BIN="$candidate" && break
    done
fi
if [ ! -x "${CLAUDE_BIN:-}" ]; then
    echo "no claude binary found (tried \$MARLOW_CLAUDE_BIN, ~/.local/bin/claude, PATH)." >&2
    echo "the deployed unit uses: systemctl show marlow-server -p Environment" >&2
    exit 1
fi

# No hooks, no permissions: a bare object. The point is to isolate the CLI's
# tool-offering behaviour from every policy layer we have built on top of it.
SETTINGS="$OUT_DIR/settings.json"
printf '{}\n' > "$SETTINGS"

# Two prompts. The enumeration is for the human reading the output; the call
# attempt is the actual evidence, because it forces a tool_use event.
PROMPT_LIST='List every tool available to you whose name begins with "mcp__". Output only the tool names, one per line, nothing else. If there are none, output exactly: NONE'
PROMPT_CALL='Use your Google Calendar tool to list my calendar events for today. If you have no such tool available, reply with exactly: NO_CALENDAR_TOOL'
PROMPT_MAIL='Use your Gmail tool to search for messages matching "newer_than:1d in:inbox". If you have no such tool available, reply with exactly: NO_GMAIL_TOOL'

# Same scrub as server/invoke.py: a probe that inherits the caller's agent env
# is measuring the caller, not the box.
scrubbed() {
    local -a unset_args=()
    while IFS='=' read -r name _; do
        case "$name" in
            CLAUDE_CODE_*|CLAUDECODE|AI_AGENT|ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN|ANTHROPIC_BASE_URL)
                unset_args+=("-u" "$name") ;;
        esac
    done < <(env)
    env "${unset_args[@]}" "$@"
}

run_probe() {
    local label="$1" prompt="$2"; shift 2   # remaining args are extra claude flags
    local out="$OUT_DIR/$label.jsonl"

    echo "--- $label ($([ $# -eq 0 ] && echo 'no --strict-mcp-config' || echo "$*")) ---"
    scrubbed "$CLAUDE_BIN" -p \
        --settings "$SETTINGS" \
        --output-format stream-json --verbose \
        "$@" \
        -- "$prompt" </dev/null > "$out" 2>"$OUT_DIR/$label.err"

    local tools
    tools="$(grep -o '"name":"mcp__[a-zA-Z0-9_]*"' "$out" 2>/dev/null | sort -u | sed 's/"name":"//;s/"$//')"
    if [ -n "$tools" ]; then
        echo "  MCP TOOLS INVOKED:"; echo "$tools" | sed 's/^/    /'
    else
        echo "  no mcp__ tool_use events"
    fi

    # The final assistant text, for the human. python3 is on the box; keep this
    # tolerant of a truncated stream rather than failing the whole probe.
    python3 - "$out" <<'PY' 2>/dev/null || echo "  (no parseable result)"
import json, sys
last = ""
for line in open(sys.argv[1]):
    try:
        ev = json.loads(line)
    except ValueError:
        continue
    if ev.get("type") == "result":
        last = ev.get("result", "")
print("  RESULT: " + " ".join(last.split())[:400])
PY
    if [ -s "$OUT_DIR/$label.err" ]; then
        echo "  STDERR: $(head -c 300 "$OUT_DIR/$label.err" | tr '\n' ' ')"
    fi
    echo
}

echo "=== probe-connectors: running as $(whoami) ==="
echo "=== binary: $CLAUDE_BIN ($("$CLAUDE_BIN" --version 2>&1 | head -1)) ==="
echo

# Control first. If this returns nothing, the account/auth is the problem and
# every result below is meaningless -- exactly the failure that made the bwrap
# probe's first two runs worthless.
#
# And ABORT on it rather than carrying on. The first run of this probe printed
# six identical failures under a banner telling the reader to check the control,
# which buries one fact under five copies of itself. A control that does not
# stop the run is decoration.
echo "### CONTROL — is the CLI working as this user at all?"
run_probe control 'Reply with exactly: OK' --strict-mcp-config
if ! grep -q '"type":"result"' "$OUT_DIR/control.jsonl" 2>/dev/null; then
    echo "ABORTING: the control produced no result event. Everything below would" >&2
    echo "be indistinguishable from a real negative -- fix the control first." >&2
    exit 1
fi

echo "### A — WITH --strict-mcp-config (what server/invoke.py does today)"
run_probe strict-list "$PROMPT_LIST" --strict-mcp-config
run_probe strict-cal  "$PROMPT_CALL" --strict-mcp-config

echo "### B — WITHOUT --strict-mcp-config"
run_probe loose-list "$PROMPT_LIST"
run_probe loose-cal  "$PROMPT_CALL"
run_probe loose-mail "$PROMPT_MAIL"

# --- Round 2 (added 2026-08-06, after round 1 returned "A empty, B populated")
#
# Round 1 framed the choice as binary: keep --strict-mcp-config and lose Gmail,
# or drop it and let the CLI honour EVERY config source -- including
# .mcp.json inside /home/marlow/marlow-identity, which Marlow can write. An
# mcpServers entry is a `command` that runs when the CLI STARTS, before any tool
# call, so the PreToolUse hook never sees it and bwrap does not stop it.
#
# `claude mcp list` showed the connectors are ordinary HTTP MCP servers at
# public URLs, which suggests a third option: DECLARE the two we want in an
# explicit --mcp-config file and KEEP --strict-mcp-config. That is an allowlist
# of exactly two servers, with every writable config source still ignored.
#
# The question is auth. These URLs are OAuth-protected and the grant lives with
# the account, not in the JSON. If an explicitly-declared server reuses the
# existing grant, this is strictly better than dropping the flag. If it demands
# an interactive login, it cannot work headless and the binary choice stands.
#
# URLs captured from `claude mcp list` as mike on 2026-08-06. They are Google's,
# not ours, and nothing guarantees they are stable -- if round 2 suddenly fails
# later, re-run `claude mcp list` before assuming a code cause.
#
# Drive is deliberately omitted: the jobs need mail and calendar. Minimal grant.
SCOPED="$OUT_DIR/mcp-scoped.json"
cat > "$SCOPED" <<'JSON'
{
  "mcpServers": {
    "claude_ai_Gmail": {
      "type": "http",
      "url": "https://gmailmcp.googleapis.com/mcp/v1"
    },
    "claude_ai_Google_Calendar": {
      "type": "http",
      "url": "https://calendarmcp.googleapis.com/mcp/v1"
    }
  }
}
JSON

# Server names match the connectors' own, so tool names come out as
# mcp__claude_ai_Gmail__* -- the same strings v1's settings.local.json already
# allows, and the same ones round 1 observed. If the scoped run produces
# differently-named tools, the permission policy has to be written against
# whatever it actually emits, so this is worth reading carefully.
echo "### C — --strict-mcp-config WITH an explicit --mcp-config allowlist"
run_probe scoped-cal  "$PROMPT_CALL" --strict-mcp-config --mcp-config "$SCOPED"
run_probe scoped-mail "$PROMPT_MAIL" --strict-mcp-config --mcp-config "$SCOPED"

cat <<'EOF'
=== HOW TO READ THIS ===

Check the CONTROL first. Anything other than "OK" means auth or the CLI is
broken for this user and NOTHING below is evidence.

Then compare A against B, on the "MCP TOOLS INVOKED" lines only:

  A empty, B has mcp__claude_ai_*      -> --strict-mcp-config is what strips
                                          them. Parity needs that flag relaxed
                                          in invoke.py. That is a CLAUDE.md
                                          hard constraint: Mike's call.

  A and B both empty                   -> the `marlow` ACCOUNT has no
                                          connectors. The flag is irrelevant;
                                          connectors must be added to that
                                          account (or the jobs that need Gmail
                                          run some other way). Check whether
                                          marlow's login is even the same
                                          account/plan as mike's.

  A and B both have mcp__claude_ai_*   -> connectors survive the flag. No code
                                          change needed; go straight to porting
                                          the jobs.

ROUND 1 VERDICT (2026-08-06, CLI 2.1.216, as marlow): A empty, B populated.
The flag strips them, and the marlow account HAS Gmail + Calendar.

Now read C, which decides whether that has to be a trade at all:

  C invokes mcp__claude_ai_*           -> BEST CASE. Keep --strict-mcp-config,
                                          add --mcp-config with just these two
                                          servers. Connectors work AND every
                                          Marlow-writable config source stays
                                          ignored. No CLAUDE.md constraint is
                                          relaxed; it gains an allowlist.

  C emits no tool_use, stderr mentions -> the declared server needs an
  auth/OAuth/401/unauthorized               interactive grant it cannot get
                                            headless. Middle path is out; the
                                            choice is back to binary, and
                                            dropping the flag has to be paired
                                            with denying writes to .mcp.json.

  C invokes DIFFERENTLY-NAMED tools    -> works, but the permission policy must
                                          be written against the names actually
                                          emitted, not the ones assumed here.

Note the enumeration runs: BOTH said NONE, including the loose run that then
went and invoked a connector tool successfully. Asked to list its own tools, the
model is not a reliable witness -- which is why this greps tool_use events. Do
not "simplify" this script by trusting the answer text.

Record the verdict, the CLI version, and the date in
docs/tasks/v2-dogfood-parity.md -- see docs/tasks/probe-suite-doctor-mode.md
for why the baseline matters more than the script.
EOF
