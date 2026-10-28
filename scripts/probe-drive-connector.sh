#!/usr/bin/env bash
# Probe: does the CLI offer the account's Google Drive connector?
#
# WHY: Taylor connected Drive alongside Gmail and Calendar at claude.ai on
# 2026-10-17, and Marlow never used it: it opened a pasted Drive link, but not
# through the connector (docs/tasks/onboarding-v2.md). probe-connectors.sh only
# ever checked Gmail and Calendar, and FIRST-RUN.md and identity/CLAUDE.md
# mention only those two, so Drive may be offered and simply unknown to the
# agent. Answer that before deciding on a fix:
#
#   offered     -> tell the identity about it, and decide which Drive read tools
#                  join the deploy/marlow-settings.json allowlist (today none
#                  do, so every Drive call would ask the phone).
#   not offered -> the connector isn't reaching the CLI; separate investigation.
#
# Same evidence rule as probe-connectors.sh: a `tool_use` event naming an
# `mcp__` tool, never the model's own list of its tools (that list said NONE in
# 2026-08-06 runs that then called a connector). A denial is a pass: we ask
# whether the tool is offered, not whether it runs.
#
# Production path: NO --strict-mcp-config, since invoke.py no longer passes it.
#
# RUN IN A CLEAN SHELL ON THE BOX, never inside a Claude Code session, AS THE
# INSTANCE USER whose account connected Drive:
#
#   sudo -u taylor -H bash /home/taylor/marlow-server/scripts/probe-drive-connector.sh
#
# Writes its own empty settings file (no hooks), so nothing reaches her phone.
# COST: 4 `claude -p` calls on that account's subscription, plus `claude mcp list`.
set -uo pipefail

OUT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/marlow-probe-drive.XXXXXX")" || exit 1
trap 'rm -rf "$OUT_DIR"' EXIT

# sudo -u ... -H gets a PATH without ~/.local/bin (see probe-connectors.sh).
CLAUDE_BIN="${MARLOW_CLAUDE_BIN:-}"
if [ -z "$CLAUDE_BIN" ]; then
    for candidate in "$HOME/.local/bin/claude" "$(command -v claude 2>/dev/null || true)"; do
        [ -x "$candidate" ] && CLAUDE_BIN="$candidate" && break
    done
fi
if [ ! -x "${CLAUDE_BIN:-}" ]; then
    echo "no claude binary found (tried \$MARLOW_CLAUDE_BIN, ~/.local/bin/claude, PATH)." >&2
    exit 1
fi

SETTINGS="$OUT_DIR/settings.json"
printf '{}\n' > "$SETTINGS"

# Same scrub as server/invoke.py.
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

# Third arg "quiet" hides the reply text: the Gmail run's reply is a summary
# of the user's inbox, which has no business on the operator's terminal
# (first run, 2026-10-17, printed one).
run_probe() {
    local label="$1" prompt="$2" quiet="${3:-}"
    local out="$OUT_DIR/$label.jsonl"
    echo "--- $label ---"
    scrubbed "$CLAUDE_BIN" -p --settings "$SETTINGS" \
        --output-format stream-json --verbose \
        -- "$prompt" </dev/null > "$out" 2>"$OUT_DIR/$label.err"
    # The init event's mcp_servers say which servers the session connected,
    # and its tools list what was loaded up front. ToolSearch calls and the
    # mcp__ names in their results show what a deferred search found. All of
    # it is the CLI's own record, not the model's account of itself.
    QUIET="$quiet" python3 - "$out" <<'PY' 2>/dev/null || echo "  (no parseable stream)"
import json, os, re, sys
names = lambda text: sorted(set(re.findall(r"mcp__[A-Za-z0-9_]+", text)))
result, invoked = "", set()
for line in open(sys.argv[1]):
    try:
        ev = json.loads(line)
    except ValueError:
        continue
    t = ev.get("type")
    if t == "system" and ev.get("subtype") == "init":
        for srv in ev.get("mcp_servers", []):
            print(f"  INIT server: {srv.get('name')} = {srv.get('status')}")
        # Connector tools are deferred, so they never appear here even when
        # they work. The count still matters: 13 against the usual 78 was
        # the startup race caught live (connector-drops.md).
        print(f"  INIT tools: {len(ev.get('tools', []))}")
    for block in (ev.get("message") or {}).get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            name = block.get("name", "")
            if name.startswith("mcp__"):
                invoked.add(name)
            elif name == "ToolSearch":
                print(f"  TOOLSEARCH query: {json.dumps(block.get('input', {}).get('query'))}")
        elif block.get("type") == "tool_result":
            found = names(json.dumps(block.get("content")))
            if found and not any(n in invoked for n in found):
                print(f"  TOOLSEARCH found: {', '.join(found)}")
    if t == "result":
        result = ev.get("result", "")
print("  MCP TOOLS INVOKED: " + (", ".join(sorted(invoked)) or "none"))
# Quiet still shows a sentinel reply (NO_DRIVE_TOOL), which carries no data.
if not os.environ.get("QUIET") or re.fullmatch(r"[A-Z_]+", result.strip()):
    print("  RESULT: " + " ".join(result.split())[:400])
else:
    print(f"  RESULT: (hidden, {len(result)} chars)")
PY
    [ -s "$OUT_DIR/$label.err" ] && echo "  STDERR: $(head -c 300 "$OUT_DIR/$label.err" | tr '\n' ' ')"
    echo
}

# Run from the user's own home: from the operator's cwd the CLI also reads
# THAT directory's .mcp.json (first run hit /home/mike/.mcp.json, EACCES).
cd "$HOME" || exit 1

echo "=== probe-drive-connector: running as $(whoami) ==="
echo "=== binary: $CLAUDE_BIN ($("$CLAUDE_BIN" --version 2>&1 | head -1)) ==="
echo

echo "### CONTROL"
run_probe control 'Reply with exactly: OK'
if ! grep -q '"type":"result"' "$OUT_DIR/control.jsonl" 2>/dev/null; then
    echo "ABORTING: the control produced no result event; nothing below would be evidence." >&2
    exit 1
fi

echo "### claude mcp list (Drive lines; full list below)"
scrubbed "$CLAUDE_BIN" mcp list </dev/null > "$OUT_DIR/mcp-list.txt" 2>&1
if grep -qi drive "$OUT_DIR/mcp-list.txt"; then
    grep -i drive "$OUT_DIR/mcp-list.txt" | sed 's/^/  /'
else
    echo "  no line mentions Drive"
fi
sed 's/^/    /' "$OUT_DIR/mcp-list.txt"
echo

# Gmail as a second control: it is known to work on this account, so if it
# shows no tool_use either, the probe is broken, not Drive.
echo "### GMAIL (known-good connector)"
run_probe gmail 'Use your Gmail tool to search for messages matching "newer_than:1d in:inbox". Use ToolSearch first if the tool is deferred. If you have no such tool, reply with exactly: NO_GMAIL_TOOL' quiet

echo "### DRIVE"
run_probe drive 'Use your Google Drive tool to list my most recently modified files. Use ToolSearch first if the tool is deferred. If you have no Google Drive tool, reply with exactly: NO_DRIVE_TOOL' quiet

# Round 2 (2026-10-17): the first run said NO_DRIVE_TOOL while mcp list showed
# Drive connected, and kept nothing to say whether the model searched at all.
# This one names the tool exactly, as it appears in a Claude Code session with
# the same connector, so only the CLI not loading it can make it fail.
echo "### DRIVE, exact name"
run_probe drive-exact 'Call ToolSearch with query "select:mcp__claude_ai_Google_Drive__list_recent_files", then call that tool with no arguments. If ToolSearch returns nothing, reply with exactly: NOT_LOADED' quiet

cat <<'EOF'
=== HOW TO READ THIS ===
CONTROL must say OK, and GMAIL must invoke an mcp__*Gmail* tool; otherwise
nothing here is evidence. Replies other than a sentinel are hidden on purpose.

DRIVE, exact name, invokes the tool  -> loaded. If plain DRIVE didn't find it,
                                        read its TOOLSEARCH lines: the model
                                        either didn't search or searched with
                                        words that missed. Fix is ours.
exact name says NOT_LOADED, and INIT -> the CLI isn't loading Drive's tools
  shows the Drive server failed or     though mcp list says Connected. Paste
  missing                              the INIT lines and STDERR.

Record the verdict, CLI version and date in docs/tasks/onboarding-v2.md.
EOF
