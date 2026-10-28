#!/usr/bin/env bash
# Probe: does `--effort` actually do anything in print mode, and is switching
# it mid-session cache-neutral?
#
# Blocking unknown for docs/tasks/model-and-effort-config.md. `--effort` is a
# declared flag on 2.1.235, but "declared" is not "honoured in -p", and the
# whole of piece 2 (the phone's per-turn effort toggle) assumes a mid-session
# switch does NOT invalidate the prompt cache. Both are measured here.
#
# RUN THIS IN A RAW TERMINAL ON THE BOX, as the `marlow` user, NOT from a
# Claude Code session -- a nested invocation inherits the harness env and gives
# false results (CLAUDE.md hard rule).
#
#   ssh marlow@marlow
#   bash ~/marlow-server/scripts/probe-effort-flag.sh
#
# It does NOT touch the live phone session; it builds a throwaway one.

set -uo pipefail

CLAUDE="${MARLOW_CLAUDE_BIN:-$HOME/.local/bin/claude}"
WORKDIR="${MARLOW_WORKING_DIR:-$HOME/marlow-identity}"
PROJDIR="$HOME/.claude/projects/-home-marlow-marlow-identity"

# Match invoke.py's scrub so this behaves like a server-driven call.
for v in $(env | grep -oE '^(CLAUDE_CODE_[A-Z_]*|CLAUDECODE|AI_AGENT)' || true); do
  unset "$v"
done
export CLAUDE_CODE_DISABLE_AUTO_MEMORY=1

cd "$WORKDIR" || exit 1

say() { printf '\n=== %s ===\n' "$*"; }

# A prompt with enough to reason about that effort has somewhere to show up.
# A trivial prompt thinks ~0 tokens at every level and proves nothing.
PROMPT='Three switches downstairs control three bulbs upstairs, and you may go
up only once. Work out a method and explain why it is correct.'

say "0. Does the flag even parse in print mode?"
"$CLAUDE" -p --effort low --output-format json -- "Reply with exactly: ack" </dev/null \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("  subtype=%s is_error=%s result=%s" % (d.get("subtype"), d.get("is_error"), str(d.get("result"))[:80]))' \
  || { echo "  FLAG REJECTED IN PRINT MODE -- stop here, piece 2 is dead as designed"; exit 1; }

say "1. Fresh session per level: does thinking_tokens move with --effort?"
# Fresh session each time so the comparison is not contaminated by history.
# NOTE: assistant records are written twice (see the task doc) -- this reads
# the single authoritative `usage` block off the result envelope, not a sum
# over the transcript, so it is not subject to the doubling caveat.
for level in low medium high; do
  "$CLAUDE" -p --effort "$level" --output-format json -- "$PROMPT" </dev/null \
    | python3 -c '
import json,sys
d=json.load(sys.stdin); u=d.get("usage",{}) or {}
det=u.get("output_tokens_details",{}) or {}
print("  effort=%-6s thinking=%-6s output=%-6s duration_ms=%s" % (
    sys.argv[1], det.get("thinking_tokens"), u.get("output_tokens"), d.get("duration_ms")))' "$level" \
    || echo "  effort=$level INVOCATION FAILED"
done
echo
echo "  Read: thinking should rise with the level, and duration with it."
echo "  If all three are identical, --effort is being accepted and IGNORED in"
echo "  print mode -- which kills piece 2 and piece 3 both. Say so plainly."

say "2. Mid-session switch: is it cache-neutral?"
SEED=$("$CLAUDE" -p --effort low --output-format json -- "Reply with exactly: ack" </dev/null)
SID=$(printf '%s' "$SEED" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("session_id",""))')
if [ -z "$SID" ]; then
  echo "FAILED to get a session_id. Raw result:"; printf '%s\n' "$SEED"; exit 1
fi
echo "session_id = $SID"

turn() { # $1 = effort, $2 = label
  "$CLAUDE" -p --effort "$1" --output-format json --resume="$SID" \
    -- "Turn $2. Describe the Rocky Mountains in about 200 words." </dev/null \
    | python3 -c '
import json,sys
d=json.load(sys.stdin); u=d.get("usage",{}) or {}
print("  %-22s cache_read=%-9s cache_create=%s" % (
    sys.argv[1], u.get("cache_read_input_tokens"), u.get("cache_creation_input_tokens")))' "$2" \
    || echo "  turn $2 FAILED"
}

turn low  "a (low, warming)"
turn low  "b (low, warm)"
turn high "c (HIGH, switched)"
turn high "d (high, warm)"
echo
echo "  Read: if turn c's cache_read is in line with b's, switching effort is"
echo "  cache-neutral and the phone's per-turn toggle is safe as designed."
echo "  If c's cache_read collapses toward zero (and cache_create jumps), the"
echo "  toggle must move to a session boundary, same as the model switch."

say "Done. Paste the whole output back into the session."
