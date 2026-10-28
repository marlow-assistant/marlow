#!/usr/bin/env bash
# Probe: does a stdio MCP server started from `--mcp-config` work inside the
# box's real invocation shape? (docs/tasks/weather-mcp.md, "Probe")
#
# Two questions, both answered by ONE init event:
#
#   Q1. Does the server get network access to Open-Meteo? It is spawned by the
#       CLI, so it inherits the CLI's bwrap namespace (server/invoke.py,
#       `_confinement_prefix`). That prefix has no --unshare-net today, but
#       measure rather than read code: egress rules or DNS could differ.
#   Q2. Do its tools appear in the init event's `tools` array ALONGSIDE the
#       Gmail and Calendar connectors -- i.e. does `--mcp-config` (without
#       --strict-mcp-config) add to the account's connectors, not replace them?
#
# HOW Q1 IS REPORTED WITHOUT A TOOL CALL. The probe server fetches both hosts
# at startup and encodes the HTTP status in its one tool's NAME, e.g.
# `mcp__weatherprobe__net_api_200_geo_200`. The init event lists it, so no
# tool runs, no permission is asked, and nothing reaches Mike's phone.
#
# EVIDENCE is the init event, never the model's own account of its tools
# (docs/marlow-v2-START.md, "Don't believe Marlow about its own harness").
#
# RUN IN A CLEAN SHELL ON THE BOX, as marlow, never inside a Claude Code
# session (a nested `claude -p` inherits the parent's env):
#
#   ssh marlow@marlow 'bash -s' < scripts/probe-weather-mcp.sh
#
# COST: 1 `claude -p` call, a one-word reply.
set -uo pipefail

OUT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/marlow-probe-weather.XXXXXX")" || exit 1
trap 'rm -rf "$OUT_DIR"' EXIT

CLAUDE_BIN="${MARLOW_CLAUDE_BIN:-$HOME/.local/bin/claude}"
PYTHON="${MARLOW_PYTHON:-$HOME/.venvs/marlow-server/bin/python}"
REPO="${MARLOW_REPO:-$HOME/marlow-server}"
for bin in "$CLAUDE_BIN" "$PYTHON"; do
    [ -x "$bin" ] || { echo "missing: $bin" >&2; exit 1; }
done

# --- the probe server: stdlib only, newline-delimited JSON-RPC on stdio ------
cat > "$OUT_DIR/probe_server.py" <<'PY'
import json, sys, urllib.request

def status(url):
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            return str(r.status)
    except urllib.error.HTTPError as e:
        return str(e.code)
    except Exception as e:  # DNS, refused, timeout: the name says which class
        return "ERR" + type(e).__name__[:12]

api = status("https://api.open-meteo.com/v1/forecast?latitude=44.5&longitude=-81.4&daily=temperature_2m_max&forecast_days=1")
geo = status("https://geocoding-api.open-meteo.com/v1/search?name=Sault+Ste.+Marie&count=1")
tool = f"net_api_{api}_geo_{geo}"

for line in sys.stdin:
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    if "id" not in msg:
        continue  # notifications
    method, result = msg.get("method"), None
    if method == "initialize":
        result = {"protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "weatherprobe", "version": "0"}}
    elif method == "tools/list":
        result = {"tools": [{"name": tool, "description": "probe only, never call",
                             "inputSchema": {"type": "object", "properties": {}}}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
    sys.stdout.flush()
PY

# Inline JSON in ONE token (`--mcp-config=...`), exactly the form invoke.py
# will pass -- the option is variadic, and a file would test a different path.
MCP_JSON="{\"mcpServers\": {\"weatherprobe\": {\"type\": \"stdio\", \"command\": \"$PYTHON\", \"args\": [\"-I\", \"$OUT_DIR/probe_server.py\"]}}}"

# --- the real confinement prefix, from the deployed server's own code -------
# Built by server.invoke._confinement_prefix under the unit's environment, so
# the probe runs in the namespace a real turn does, not an approximation.
PREFIX_FILE="$OUT_DIR/prefix"
(
    cd "$REPO" || exit 1
    while IFS= read -r kv; do export "$kv"; done < <(
        systemctl show marlow-server -p Environment --value | tr ' ' '\n' | grep '=')
    "$PYTHON" -c 'import sys
from server.invoke import _confinement_prefix
sys.stdout.write("\0".join(_confinement_prefix()))' > "$PREFIX_FILE"
) || { echo "could not build the confinement prefix" >&2; exit 1; }
PREFIX=()
[ -s "$PREFIX_FILE" ] && mapfile -d '' -t PREFIX < "$PREFIX_FILE"
echo "=== confinement: ${PREFIX[*]:-(none -- bwrap disabled)}"
echo "=== claude: $("$CLAUDE_BIN" --version 2>&1 | head -1)"

# Same scrub as server/invoke.py.
unset_args=()
while IFS='=' read -r name _; do
    case "$name" in
        CLAUDE_CODE_*|CLAUDECODE|AI_AGENT|ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN|ANTHROPIC_BASE_URL)
            unset_args+=("-u" "$name") ;;
    esac
done < <(env)

OUT="$OUT_DIR/run.jsonl"
cd "$HOME/marlow-identity" || exit 1
env "${unset_args[@]}" "${PREFIX[@]}" "$CLAUDE_BIN" -p \
    --output-format stream-json --verbose \
    "--mcp-config=$MCP_JSON" \
    -- 'Reply with exactly: OK' </dev/null > "$OUT" 2>"$OUT_DIR/err"

"$PYTHON" - "$OUT" <<'PY'
import json, sys
init = None
for line in open(sys.argv[1]):
    try:
        ev = json.loads(line)
    except ValueError:
        continue
    if ev.get("type") == "system" and ev.get("subtype") == "init":
        init = ev
        break
if init is None:
    print("FAIL: no init event -- see stderr below"); sys.exit(1)
tools = init.get("tools", [])
servers = init.get("mcp_servers", [])
print("mcp_servers:", json.dumps(servers))
probe = [t for t in tools if t.startswith("mcp__weatherprobe__")]
gmail = [t for t in tools if "Gmail" in t]
cal = [t for t in tools if "Calendar" in t]
print("probe tool:", probe or "ABSENT")
print(f"gmail tools: {len(gmail)}, calendar tools: {len(cal)}")
ok = probe == ["mcp__weatherprobe__net_api_200_geo_200"] and gmail and cal
print("VERDICT:", "PASS" if ok else "FAIL")
PY
[ -s "$OUT_DIR/err" ] && echo "STDERR: $(head -c 400 "$OUT_DIR/err" | tr '\n' ' ')"
