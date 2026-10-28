#!/usr/bin/env bash
# Probe: can a headless `claude -p --resume` session be compacted?
#
# This is the blocking unknown for idle-triggered auto-compaction
# (docs/tasks/nightly-consolidation.md piece 1 / the idle-compact design).
# `/compact` is documented as an interactive slash command; whether print mode
# honours it is undocumented and must be measured, not assumed.
#
# RUN THIS IN A RAW TERMINAL ON THE BOX, as the `marlow` user, NOT from a
# Claude Code session -- a nested invocation inherits the harness env and gives
# false results (CLAUDE.md hard rule).
#
#   ssh marlow@marlow
#   bash ~/marlow-server/scripts/probe-headless-compact.sh
#
# It does NOT touch the live phone session. It builds a throwaway session,
# grows it, then tries three ways to compact it.

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
size_of() { [ -f "$1" ] && wc -c <"$1" || echo 0; }

say "1. Seed a throwaway session"
SEED=$("$CLAUDE" -p --output-format json -- "Reply with exactly: ack" </dev/null)
SID=$(printf '%s' "$SEED" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("session_id",""))')
if [ -z "$SID" ]; then
  echo "FAILED to get a session_id. Raw result:"; printf '%s\n' "$SEED"; exit 1
fi
echo "session_id = $SID"
TRANSCRIPT="$PROJDIR/$SID.jsonl"

say "2. Grow it (5 turns), so a compaction has something to do"
for i in 1 2 3 4 5; do
  "$CLAUDE" -p --output-format json --resume="$SID" \
    -- "Turn $i. Describe the Rocky Mountains in about 200 words." </dev/null \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); u=d.get("usage",{}) or {}; print("  turn ok, cache_read=%s cache_create=%s" % (u.get("cache_read_input_tokens"), u.get("cache_creation_input_tokens")))' \
    || echo "  turn $i FAILED"
done
BEFORE=$(size_of "$TRANSCRIPT")
echo "transcript before: $BEFORE bytes"

say "3a. Try /compact as the whole prompt"
"$CLAUDE" -p --output-format json --resume="$SID" -- "/compact" </dev/null \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("  subtype=%s is_error=%s" % (d.get("subtype"), d.get("is_error"))); print("  result:", str(d.get("result"))[:400])' \
  || echo "  /compact INVOCATION FAILED"
AFTER_A=$(size_of "$TRANSCRIPT")
echo "transcript after 3a: $AFTER_A bytes (was $BEFORE)"

say "3b. Try /compact with an instruction argument"
"$CLAUDE" -p --output-format json --resume="$SID" \
  -- "/compact Keep only decisions and open questions." </dev/null \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("  subtype=%s is_error=%s" % (d.get("subtype"), d.get("is_error"))); print("  result:", str(d.get("result"))[:400])' \
  || echo "  FAILED"
AFTER_B=$(size_of "$TRANSCRIPT")
echo "transcript after 3b: $AFTER_B bytes"

say "3c-env. Does the CLI's OWN auto-compaction work in print mode?"
# If this works, most of the cost problem is a one-line _FORCED_ENV change in
# invoke.py rather than a build (docs/tasks/idle-compaction.md). Forcing the
# percentage very low makes it fire on a small session instead of needing 50K+.
echo "  CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=1, one more turn:"
CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=1 "$CLAUDE" -p --output-format json --resume="$SID" \
  -- "Turn 6. Describe the Bow River in about 200 words." </dev/null \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); u=d.get("usage",{}) or {}; print("  is_error=%s cache_read=%s" % (d.get("is_error"), u.get("cache_read_input_tokens")))' \
  || echo "  FAILED -- the var may be rejected outright"
AFTER_ENV=$(size_of "$TRANSCRIPT")
echo "transcript after 3c-env: $AFTER_ENV bytes"

say "3d. Did a compaction marker ever land in the transcript?"
grep -o 'isCompactSummary\|compactMetadata\|"type":"summary"' "$TRANSCRIPT" 2>/dev/null \
  | sort | uniq -c || echo "  (no markers found)"

say "4. Does the session still work after all that?"
"$CLAUDE" -p --output-format json --resume="$SID" \
  -- "In one short sentence, what have we been discussing?" </dev/null \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); u=d.get("usage",{}) or {}; print("  is_error=%s cache_read=%s" % (d.get("is_error"), u.get("cache_read_input_tokens"))); print("  result:", str(d.get("result"))[:300])' \
  || echo "  POST-COMPACT RESUME FAILED -- that would be disqualifying"

say "VERDICT — what to look for"
cat <<'EOF'
  * Transcript SHRANK, or a compaction marker appeared, and step 4 still
    resumed cleanly  -> headless /compact works; build on it directly.
  * `result` reads like Marlow ANSWERING a message about compacting (rather
    than the CLI acting on it) -> slash commands are not honoured in print
    mode. Fall back to summarise-and-reseed: our own summary call, then a
    fresh session carrying the summary forward.
  * Step 4 fails -> compaction wedges a resumable session. Disqualifying for
    an unattended timer; do not build the idle trigger on it.

  Throwaway session left in place for inspection:
EOF
echo "    $TRANSCRIPT"
echo "  Remove with: rm -f '$TRANSCRIPT'"
