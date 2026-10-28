#!/usr/bin/env bash
# Probe: can an env var make `claude -p` wait for the claude.ai connectors?
#
# WHY: some turns start before Gmail and Calendar have loaded, and a short
# `email-flash` then replies `[silent]` (docs/tasks/connector-drops.md). Caught
# live 2026-10-17: a stream `init` event with every claude.ai server `pending`.
#
# Read statically out of the 2.1.301 binary (not run), 2026-10-18:
#   * `MCP_CONNECTION_NONBLOCKING` -- the CLI computes `nonBlocking =
#     env !== false`, so connecting is NON-blocking unless this is set false,
#     and the claude.ai connectors are then connected with `deferConnect`.
#   * `CLAUDE_CODE_MCP_STARTUP_WAIT_MS` -- overrides the first-turn hold's
#     deadline ("host_wait"). Without it a `-p` run passing `--mcp-config` (as
#     invoke.py does, for weather) gets `MCP_TIMEOUT`, 30 s by default, yet
#     connectors were still pending -- so the hold may skip deferred ones.
#   * `CLAUDE_CODE_MCP_CONNECTOR_PREWAIT_MS` / `_PREWAIT_SERVERS` only apply to
#     cloud workers (`sdkUrl`), so they are not tried here.
# Reading minified code does not say which of these actually closes the race,
# or what it costs in latency. This does, per arm:
#
#   INIT     how many claude_ai servers were connected / pending at init
#   GMAIL    whether `select:` on an exact Gmail tool name found it -- the thing
#            email-flash actually needs, independent of what init said
#   t_init   seconds from launch to the init event
#   t_first  seconds from launch to the first assistant message -- the latency
#            a chat turn would pay. Compare arms; this is the cost of waiting.
#
# Mirrors production where it matters: no --strict-mcp-config, an inline
# --mcp-config (it changes the hold's deadline), the invoke.py env scrub.
# Haiku and a one-line prompt keep it cheap; the model doesn't affect startup.
#
# RUN IN A CLEAN SHELL ON THE BOX, never inside a Claude Code session, as the
# account whose connectors are in question:
#
#   scp scripts/probe-connector-wait.sh marlow@marlow:/tmp/
#   ssh marlow@marlow 'bash /tmp/probe-connector-wait.sh 8'
#
# The argument is runs per arm (default 8). The race showed in 1 of 4 runs on
# 2026-10-17 but in only ~1 of 10 real turns, so a quiet baseline proves little;
# raise N, or rerun, before calling an arm a fix.
# Writes its own empty settings file (no hooks), so nothing reaches the phone.
# COST: 3 x N short Haiku `claude -p` calls on that account's subscription.
set -uo pipefail

RUNS="${1:-8}"
OUT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/marlow-probe-wait.XXXXXX")" || exit 1
trap 'rm -rf "$OUT_DIR"' EXIT

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
# An empty inline config still sets the CLI's explicitMcpConfigFlag, which is
# what moves the first-turn deadline to MCP_TIMEOUT in production.
MCP_CONFIG='{"mcpServers":{}}'

# Same scrub as server/invoke.py. The arm's own variables are added AFTER the
# scrub (via `env` in run_one), or the CLAUDE_CODE_* one would be stripped.
scrubbed() {
    local -a unset_args=()
    while IFS='=' read -r name _; do
        case "$name" in
            CLAUDE_CODE_*|CLAUDECODE|AI_AGENT|ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN|ANTHROPIC_BASE_URL|MCP_*)
                unset_args+=("-u" "$name") ;;
        esac
    done < <(env)
    env "${unset_args[@]}" "$@"
}

PROMPT='Call ToolSearch with query "select:mcp__claude_ai_Gmail__search_threads". Do not call any other tool. If it returned that tool, reply with exactly GMAIL_YES, otherwise exactly GMAIL_NO.'

# Timestamps each stream line as it arrives, then summarises one run on one
# line. Only sentinels and counts are printed, never content.
SUMMARISE='
import json, sys, time
start = float(sys.argv[1]); t_init = t_first = None
connected = pending = other = tools = 0; result = ""
for line in sys.stdin:
    now = time.time() - start
    try: ev = json.loads(line)
    except ValueError: continue
    t = ev.get("type")
    if t == "system" and ev.get("subtype") == "init" and t_init is None:
        t_init = now; tools = len(ev.get("tools", []))
        for s in ev.get("mcp_servers", []):
            if "claude_ai" not in (s.get("name") or "").replace(".", "_").replace(" ", "_"): continue
            st = s.get("status")
            if st == "connected": connected += 1
            elif st == "pending": pending += 1
            else: other += 1
    elif t == "assistant" and t_first is None:
        t_first = now
    elif t == "result":
        result = (ev.get("result") or "").strip()
gmail = result if result in ("GMAIL_YES", "GMAIL_NO") else "?(%d chars)" % len(result)
f = lambda x: "%5.1f" % x if x is not None else "    -"
print("INIT claude_ai connected=%2d pending=%2d other=%d tools=%3d | GMAIL %-9s | t_init %s t_first %s total %5.1f"
      % (connected, pending, other, tools, gmail, f(t_init), f(t_first), time.time() - start))
'

run_one() {
    local label="$1"; shift
    local start; start="$(python3 -c 'import time; print(time.time())')"
    scrubbed env "$@" "$CLAUDE_BIN" -p --model haiku --settings "$SETTINGS" \
        --mcp-config="$MCP_CONFIG" \
        --output-format stream-json --verbose \
        -- "$PROMPT" </dev/null 2>>"$OUT_DIR/$label.err" \
        | python3 -c "$SUMMARISE" "$start"
}

run_arm() {
    local label="$1"; shift
    echo "### $label ($*)"
    for i in $(seq 1 "$RUNS"); do
        printf '  %2d  ' "$i"
        run_one "$label" "$@"
    done
    [ -s "$OUT_DIR/$label.err" ] && echo "  STDERR: $(head -c 300 "$OUT_DIR/$label.err" | tr '\n' ' ')"
    echo
}

# From the user's home, as probe-drive-connector.sh does: the operator's cwd
# may have its own .mcp.json.
cd "$HOME" || exit 1

echo "=== probe-connector-wait: $(whoami), $RUNS runs per arm, $(date -u +%FT%TZ) ==="
echo "=== binary: $CLAUDE_BIN ($("$CLAUDE_BIN" --version 2>&1 | head -1)) ==="
echo

# Interleaving would spread any time-of-day effect evenly; arms in sequence
# are easier to read and the effect has not been seen. Kept simple.
run_arm baseline         PROBE_ARM=baseline
run_arm nonblocking-off  MCP_CONNECTION_NONBLOCKING=false
run_arm startup-wait     CLAUDE_CODE_MCP_STARTUP_WAIT_MS=20000

cat <<'EOF'
=== HOW TO READ THIS ===
A row with pending>0 or GMAIL_NO is a race hit. The race is intermittent, so:
  baseline has hits, an arm has none   -> that arm is a candidate fix; rerun
                                          with a bigger N before trusting it.
  baseline has none                    -> inconclusive; rerun, ideally near :00
                                          when cron fires.
  every arm has hits                   -> no env var closes it; fall back to
                                          the prompt retry or a harness retry.
Cost of waiting: compare t_first between baseline and the arm. That is what a
chat turn would pay if the arm were applied to it.
Paste the whole output into docs/tasks/connector-drops.md with the CLI version.
EOF
